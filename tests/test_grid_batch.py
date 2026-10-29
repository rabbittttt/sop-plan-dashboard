"""Main-table saves are atomic and safe to retry, including catalog changes."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from server import make_handler
from sop_store import ConflictError, SOPStore, TZ, ValidationError
from sop_workbook import WorkbookSync
from tests.test_catalog_api import SheetSync


class GridBatchTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sop-grid-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "shared_config.py"
        self.config.write_text("MERGE_GROUPS = {'智界 Together 2026款汇总': ['智界 A 2026款', '智界 B 2026款']}\n", encoding="utf-8")
        rule_patch = patch("sop_source_rules.DEFAULT_CONFIG_PATH", self.config)
        rule_patch.start()
        self.addCleanup(rule_patch.stop)
        source = self.root / "sales.json"
        source.write_text(json.dumps({"meta": {}, "products": [
            {"id": pid, "brand": "智界", "model": pid.upper(), "generation": f"智界 {pid.upper()} 2026款", "version": "Max"}
            for pid in ("a", "b", "c")], "sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", source, lambda: datetime(2026, 10, 29, 12, tzinfo=TZ))
        self.addCleanup(lambda: self.store.close())
        self.store.save_plans({"owner": "original", "notes": "保留原说明", "changes": [
            {"product_id": pid, "month": "2026-10", "sop": value, "forecast": 100 + value, "revision": 0}
            for pid, value in (("a", 10), ("b", 20))]})

    def body(self, **changes):
        view = self.store.generation_view()
        return {"request_id": str(uuid.uuid4()), "owner": "网页维护人", "generation_mode": view["mode"],
                "generation_rule_signature": view["rule_signature"], "changes": [], "catalog_changes": [], "entries": [], **changes}

    def product(self, pid):
        return next(p for p in self.store.read_sales(include_excluded=True)["products"] if p["id"] == pid)

    def plan(self, pid="a", month="2026-10"):
        row = self.store.db.execute("SELECT * FROM plans WHERE product_id=? AND month=?", (pid, month)).fetchone()
        return dict(row) if row else None

    def edit(self, sop=11, pid="a", month="2026-10"):
        return {"product_id": pid, "month": month, "sop": sop, "revision": (self.plan(pid, month) or {}).get("revision", 0)}

    def entry(self, client_id="new", model="New 2027款"):
        return {"client_id": client_id, "brand": "智界", "model": model, "version": "Plus",
                "plans": [{"month": "2026-11", "sop": 0}, {"month": "2027-01", "sop": 12}]}

    def delete(self, pid="b"):
        return {"id": pid, "revision": self.product(pid)["revision"], "active": False,
                "plan_revisions": {r["month"]: r["revision"] for r in self.store.db.execute("SELECT month,revision FROM plans WHERE product_id=?", (pid,))}}

    def tables(self):
        return {name: [tuple(r) for r in self.store.db.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                for name in ("product_catalog", "plans", "events", "catalog_versions", "grid_batch_requests")}

    def test_one_save_edits_renames_adds_and_deactivates_without_erasing_history(self):
        original_b = self.plan("b")
        result = self.store.save_grid_batch(self.body(changes=[self.edit()], entries=[self.entry()], catalog_changes=[
            {"id": "a", "revision": self.product("a")["revision"], "version": "Premium"}, self.delete()]))
        self.assertEqual((result["saved_count"], result["created_count"], result["deactivated_count"]), (3, 1, 1))
        self.assertEqual((self.plan()["sop"], self.plan()["forecast"], self.plan()["notes"]), (11, 110, "保留原说明"))
        self.assertEqual(self.product("a")["version"], "Premium")
        self.assertFalse(self.product("b")["active"])
        self.assertEqual(self.plan("b"), original_b)
        pid = result["entry_ids"]["new"]
        self.assertEqual(self.plan(pid, "2026-11")["sop"], 0)
        self.assertEqual(self.plan(pid, "2027-01")["sop"], 12)
        self.assertEqual(len(self.store.history("a")["items"]), 2)

    def test_invalid_new_cell_rolls_back_other_changes_and_receipt(self):
        before = self.tables()
        entry = self.entry(); entry["plans"][0]["sop"] = -1
        with self.assertRaises(ValidationError):
            self.store.save_grid_batch(self.body(changes=[self.edit()], catalog_changes=[self.delete()], entries=[entry]))
        self.assertEqual(self.tables(), before)

    def test_stale_cell_or_catalog_revision_rolls_back_complete_batch(self):
        for field in ("plan", "catalog"):
            with self.subTest(field=field):
                request = self.body(changes=[self.edit()], entries=[self.entry()], catalog_changes=[self.delete()])
                if field == "plan": request["changes"][0]["revision"] = 0
                else: request["catalog_changes"][0]["revision"] = 0
                before = self.tables()
                with self.assertRaises(ConflictError): self.store.save_grid_batch(request)
                self.assertEqual(self.tables(), before)

    def test_deletion_checks_unseen_month_revisions_and_rejects_missing_baseline(self):
        request = self.body(changes=[self.edit()], entries=[self.entry()], catalog_changes=[self.delete()])
        self.store.save_plans({"owner": "another", "changes": [{"product_id": "b", "month": "2027-12", "sop": 77, "forecast": None, "revision": 0}]})
        before = self.tables()
        with self.assertRaises(ConflictError): self.store.save_grid_batch(request)
        self.assertEqual(self.tables(), before)
        request["catalog_changes"][0].pop("plan_revisions")
        with self.assertRaises(ValidationError): self.store.save_grid_batch(request)
        self.assertEqual(self.tables(), before)

    def test_retry_after_restart_returns_same_result_and_ids(self):
        request = self.body(changes=[self.edit()], entries=[self.entry()], catalog_changes=[self.delete()])
        first = self.store.save_grid_batch(request)
        before = self.tables()
        self.store.close()
        self.store = SOPStore(self.root / "test.sqlite3", self.root / "sales.json", lambda: datetime(2026, 10, 29, 12, tzinfo=TZ))
        self.assertEqual(self.store.save_grid_batch(copy.deepcopy(request)), first)
        self.assertEqual(self.tables(), before)
        request["changes"][0]["sop"] = 99
        with self.assertRaisesRegex(ValidationError, "同一保存编号"):
            self.store.save_grid_batch(request)
        self.assertEqual(self.tables(), before)

    def test_readonly_hidden_ids_and_stale_rules_cannot_be_saved(self):
        stale = self.body(changes=[self.edit()])
        self.store.update_settings({"generation_mode": "summary"})
        with self.assertRaisesRegex(ValidationError, "代际范围"): self.store.save_grid_batch(stale)
        virtual = next(p for p in self.store.state()["products"] if p.get("read_only"))
        for pid in ("a", virtual["id"]):
            with self.subTest(pid=pid), self.assertRaises(ValidationError):
                self.store.save_grid_batch(self.body(changes=[{"product_id": pid, "month": "2026-10", "sop": 1, "revision": 0}]))
        request = self.body(changes=[self.edit(pid="c")])
        self.config.write_text("MERGE_GROUPS = {}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "共享规则"): self.store.save_grid_batch(request)

    def test_new_entries_cannot_duplicate_rows_or_enter_a_hidden_readonly_group(self):
        request = self.body(entries=[{**self.entry(), "model": "A 2026款", "version": "Max"}])
        with self.assertRaisesRegex(ValidationError, "已存在"): self.store.save_grid_batch(request)
        with self.assertRaises(ValidationError): self.store.save_grid_batch(self.body(entries=[self.entry(), self.entry()]))
        self.store.update_settings({"generation_mode": "summary"})
        with self.assertRaisesRegex(ValidationError, "切换至单代际"):
            self.store.save_grid_batch(self.body(entries=[{**self.entry(), "model": "A 2026款"}]))
        result = self.store.save_grid_batch(self.body(entries=[self.entry()]))
        self.assertEqual(result["created_count"], 1)

    def test_plan_range_numeric_validation_and_deleted_cell_are_atomic(self):
        bad = [self.body(changes=[self.edit(pid="c", month="2028-01")]),
               self.body(changes=[self.edit(sop=True)]),
               self.body(changes=[self.edit(pid="b")], catalog_changes=[self.delete()])]
        before = self.tables()
        for request in bad:
            with self.assertRaises(ValidationError): self.store.save_grid_batch(request)
            self.assertEqual(self.tables(), before)

    def test_rules_changing_between_view_and_validation_leave_values_untouched(self):
        request = self.body(changes=[self.edit()])
        before = self.tables()
        original = self.store.read_sales
        def changed():
            result = original()
            self.config.write_text("MERGE_GROUPS = {}\n", encoding="utf-8")
            return result
        with patch.object(self.store, "read_sales", side_effect=changed), self.assertRaisesRegex(ValidationError, "规则正在变化"):
            self.store.save_grid_batch(request)
        self.assertEqual(self.tables(), before)

    def test_real_workbook_sync_allows_web_only_edits_additions_and_deletions(self):
        maintenance = self.root / "input_file" / "SOP计划维护.xlsx"
        writer = Mock(side_effect=AssertionError("Web-only maintenance must not generate Excel"))
        sync = WorkbookSync(self.store, maintenance, writer=writer, root=self.root, auto_create=False)
        self.assertEqual(sync.sync()["status"], "absent")
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        http_server.RequestHandlerClass = make_handler(self.store, self.root, http_server.server_port, sync)
        thread = threading.Thread(target=lambda: http_server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", http_server.server_port)
        try:
            request = self.body(changes=[self.edit(25)], entries=[self.entry()], catalog_changes=[self.delete()])
            connection.request("POST", "/api/plans/batch-edit", json.dumps(request), {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 200, result)
            self.assertEqual(result["workbook_sync"]["status"], "absent")
            self.assertEqual(self.plan()["sop"], 25)
            self.assertEqual(self.plan(result["entry_ids"]["new"], "2027-01")["sop"], 12)
            self.assertFalse(self.product("b")["active"])
            self.assertEqual(sync.sync()["status"], "absent")
            writer.assert_not_called()
            self.assertFalse(maintenance.exists())
        finally:
            connection.close()
            http_server.shutdown()
            thread.join()
            http_server.server_close()

    def test_http_sync_precedes_revision_check_and_absent_file_is_not_created(self):
        sync = SheetSync(status="absent")
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        http_server.RequestHandlerClass = make_handler(self.store, self.root, http_server.server_port, sync)
        thread = threading.Thread(target=lambda: http_server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        def request(body):
            conn = http.client.HTTPConnection("127.0.0.1", http_server.server_port)
            conn.request("POST", "/api/plans/batch-edit", json.dumps(body), {"Content-Type": "application/json"})
            response = conn.getresponse(); result = response.status, json.loads(response.read()); conn.close()
            return result
        try:
            first = self.body(entries=[self.entry()])
            code, result = request(first)
            self.assertEqual(code, 200, result)
            self.assertEqual((sync.calls, result["workbook_sync"]["status"]), (2, "absent"))
            self.assertFalse((self.root / "input_file" / "SOP计划维护.xlsx").exists())
            sync.value = "pending"
            code, retried = request(first)
            self.assertEqual(code, 200)
            self.assertEqual(retried["entry_ids"], result["entry_ids"])
            current = self.body(changes=[self.edit()], entries=[self.entry("another", "Other 2027款")])
            original_sync = sync.sync
            def synchronize_edit():
                self.store.save_plans({"owner": "Excel", "changes": [{"product_id": "a", "month": "2026-10",
                                          "sop": 33, "forecast": 110, "revision": self.plan()["revision"]}]})
                return original_sync()
            with patch.object(sync, "sync", side_effect=synchronize_edit):
                code, result = request(current)
            self.assertEqual(code, 409, result)
            self.assertEqual(self.plan()["sop"], 33)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM grid_batch_requests").fetchone()[0], 1)
            sync.value = "conflict"
            code, _ = request(self.body(entries=[self.entry("blocked", "Blocked")]))
            self.assertEqual(code, 409)
        finally:
            http_server.shutdown(); thread.join(); http_server.server_close()


if __name__ == "__main__":
    unittest.main()
