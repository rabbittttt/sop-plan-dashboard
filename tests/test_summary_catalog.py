"""Hide source summary generations without losing old workbook identities."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl

from sop_roundtrip import PlanRoundtrip
from sop_store import SOPStore, TZ, ValidationError
from sop_workbook import WorkbookSync
from sop_xlsx_export import write_export_workbook
from sop_xlsx_plan import write_plan_workbook
from tests import test_inline_renderer as fixtures
from tests.test_workbook_sync import edit_cell


PRODUCTS = [
    {"id": "a", "brand": "问界", "model": "M5", "version": "Max", "generation": "问界 M5 2025款", "active": True},
    {"id": "b", "brand": "问界", "model": "M5", "version": "Max", "generation": "问界 M5 2026款", "active": True},
    {"id": "summary", "brand": "问界", "model": "M5汇总", "version": "Max", "generation": "问界 M5 汇总", "active": True},
    {"id": "manual", "brand": "智界", "model": "手工汇总", "version": "Max", "active": True},
]
PLANS = [{"product_id": p["id"], "month": "2026-10", "sop": (i+1)*10, "revision": 1}
         for i, p in enumerate(PRODUCTS)]
LOOKUP = {(p["product_id"], p["month"]): p for p in PLANS}


def previous_view():
    return patch("sop_store.project_generation_sources", lambda data, *args, **kwargs: (data, []))


class SummaryCatalogTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sop-summary-catalog-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / "shared_config.py"
        config.write_text("MERGE_GROUPS = " + repr({
            "问界 M5 汇总": ["问界 M5 2025款", "问界 M5 2026款"],
            "智界 S10 2026款汇总": ["智界 S10 2026款"]}), encoding="utf-8")
        rule_patch = patch("sop_source_rules.DEFAULT_CONFIG_PATH", config)
        rule_patch.start()
        self.addCleanup(rule_patch.stop)
        self.source = self.root / "sales.json"
        self.data = {"meta": {}, "products": PRODUCTS,
                     "sales": [{"product_id": p["id"], "month": "2026-10", "metric": "locked_orders", "value": (i+1)*100}
                               for i, p in enumerate(PRODUCTS[:3])],
                     "model_sales": [{"generation": p["generation"], "month": "2026-10", "metric": "locked_orders", "value": (i+1)*100}
                                     for i, p in enumerate(PRODUCTS[:3])]}
        self.source.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        with previous_view():
            self.store = SOPStore(self.root / "test.sqlite3", self.source,
                                  lambda: datetime(2026, 10, 28, 12, tzinfo=TZ))
            self.store.save_plans({"owner": "test", "changes": [
                {**plan, "forecast": None, "revision": 0} for plan in PLANS]})
            self.store.save_shipments({"owner": "test", "changes": [
                {"product_id": p["id"], "month": "2026-10", "quantity": i+1, "status": "settled",
                 "source": "SP", "batch": "", "revision": 0} for i, p in enumerate(PRODUCTS)]})
        self.addCleanup(self.store.close)

    def quantity(self, pid):
        return self.store.db.execute("SELECT sop FROM plans WHERE product_id=? AND month='2026-10'", (pid,)).fetchone()[0]

    def test_existing_cached_summary_is_hidden_but_not_deleted(self):
        before_source = self.source.read_bytes()
        state = self.store.state()
        self.assertEqual({p["id"] for p in state["products"]}, {"a", "b", "manual"})
        self.assertEqual({p["product_id"] for p in state["plans"]}, {"a", "b", "manual"})
        self.assertEqual({p["product_id"] for p in state["shipments"]}, {"a", "b", "manual"})
        self.assertEqual(sum(p["value"] for p in state["sales"]), 300)
        self.assertEqual(sum(p["value"] for p in state["model_sales"]), 300)
        self.assertEqual(self.store.matching("2026-10")["product_count"], 3)
        self.assertEqual(self.quantity("summary"), 30)
        self.assertEqual(self.store.db.execute("SELECT active FROM product_catalog WHERE id='summary'").fetchone()[0], 1)
        self.assertEqual(len(self.store.read_sales(include_excluded=True)["products"]), 4)
        self.assertEqual(self.source.read_bytes(), before_source)
        raw_version = json.loads(self.store.db.execute("SELECT payload FROM sales_versions ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertIn("summary", {p["id"] for p in raw_version["products"]})

    def test_absent_summary_source_stays_hidden_without_losing_history(self):
        fresh = {**self.data, "products": [p for p in PRODUCTS if p["id"] != "summary"],
                 "sales": self.data["sales"][:2], "model_sales": self.data["model_sales"][:2]}
        self.source.write_text(json.dumps(fresh), encoding="utf-8")
        visible = self.store.list_products()["items"]
        self.assertNotIn("summary", {p["id"] for p in visible})
        self.assertEqual(self.quantity("summary"), 30)
        hidden = next(p for p in self.store.read_sales(include_excluded=True)["products"] if p["id"] == "summary")
        self.assertFalse(hidden["source_present"])
        self.assertTrue(hidden["active"])

    def test_summary_source_does_not_claim_a_matching_manual_identity(self):
        created = self.store.save_products({"owner": "test", "changes": [
            {"brand": "智界", "model": "S10", "version": "Max", "active": True}]})["items"][0]
        summary = {"id": "new-summary", "brand": "智界", "model": "S10", "version": "Max",
                   "generation": "智界 S10 2026款汇总"}
        data = {**self.data, "products": [*PRODUCTS, summary]}
        self.source.write_text(json.dumps(data), encoding="utf-8")
        visible = self.store.list_products()["items"]
        self.assertNotIn("new-summary", self.store.source_aliases())
        manual = next(p for p in visible if p["id"] == created["id"])
        self.assertFalse(manual["source_backed"])
        self.assertNotIn("new-summary", {p["id"] for p in visible})
        # A subsequent real source can still bind; the hidden catalog row
        # must not create a spurious same-name ambiguity.
        actual = {**summary, "id": "new-real", "generation": "智界 S10 2026款"}
        self.source.write_text(json.dumps({**data, "products": [*PRODUCTS, summary, actual]}), encoding="utf-8")
        self.store.read_sales()
        self.assertEqual(self.store.source_aliases().get("new-real"), created["id"])
        self.assertNotIn("new-summary", self.store.source_aliases())

    def test_legacy_mixed_alias_keeps_real_product_without_double_counting(self):
        summary = {"id": "summary-alias", "brand": "问界", "model": "M5", "version": "Max",
                   "generation": "问界 M5 汇总"}
        data = {**self.data, "products": [*PRODUCTS, summary],
                "sales": [*self.data["sales"], {"product_id": "summary-alias", "month": "2026-10", "metric": "locked_orders", "value": 999}]}
        self.source.write_text(json.dumps(data), encoding="utf-8")
        self.store.read_sales()
        # Model an older confirmed binding; no production database is touched.
        self.store.db.execute("INSERT INTO source_aliases VALUES (?,?,?,?)",
                              ("summary-alias", "a", "2026-10-28T12:00:00+08:00", "old binding"))
        visible = self.store.read_sales()
        real = next(p for p in visible["products"] if p["id"] == "a")
        self.assertEqual(real["generation"], "问界 M5 2025款")
        self.assertEqual(real["source_generations"], ["问界 M5 2025款"])
        self.assertEqual(sum(row["value"] for row in visible["sales"]), 300)
        self.assertEqual(sum(row["value"] for row in visible["model_sales"]), 300)
        self.assertEqual(self.quantity("a"), 10)
        self.assertEqual(len(self.store.read_sales(include_excluded=True)["products"]), 4)
        self.assertTrue(self.store.manual_snapshot("mixed-source-check")["ok"])

    def test_old_maintenance_imports_saved_edits_then_removes_summary_rows(self):
        path = self.root / "maintenance.xlsx"
        with previous_view():
            previous = WorkbookSync(self.store, path, writer=write_plan_workbook)
            self.assertEqual(previous.sync()["status"], "synced")
            token, context, *_ = previous._parse(path.read_bytes())
            cells = {item["product_id"]: item["cell"] for item in context["layout"]["cells"]
                     if item["month"] == "2026-10"}
        edit_cell(path, cells["a"], 11)
        edit_cell(path, cells["summary"], 31)
        current = WorkbookSync(self.store, path, writer=write_plan_workbook)
        result = current.sync()
        self.assertEqual(result["status"], "synced", result)
        new_token, context, *_ = current._parse(path.read_bytes())
        self.assertNotEqual(new_token, token)
        self.assertNotIn("summary", {p["id"] for p in context["products"]})
        self.assertEqual(self.quantity("a"), 11)
        self.assertEqual(self.quantity("summary"), 31)
        self.assertEqual(current.sync()["status"], "synced")
        self.assertEqual(self.store.db.execute("SELECT active FROM product_catalog WHERE id='summary'").fetchone()[0], 1)

    def old_download(self):
        with patch.multiple(fixtures, PRODUCTS=PRODUCTS, PLANS=PLANS, LOOKUP=LOOKUP):
            body = fixtures.export_payload(months=["2026-10"])
        body["selected_product_ids"] = [p["id"] for p in PRODUCTS]
        body["row_product_ids"] = body.pop("roundtrip")["row_product_ids"]
        service = PlanRoundtrip(self.store)
        with previous_view():
            clean, context = service.prepare(body)
        path = self.root / "old-export.xlsx"
        write_export_workbook({**clean, "roundtrip": context, "output_path": str(path)})
        service.register(path, context)
        book = openpyxl.load_workbook(path)
        try:
            spec = fixtures.manifest(book)
            rows = {r["product_id"]: r["row"] for r in spec["rows"] if r.get("product_id") and r.get("metric") == "SOP计划量"}
        finally:
            book.close()
        return service, path, rows, body

    def test_old_download_retains_hidden_identity_on_reimport(self):
        service, path, rows, body = self.old_download()
        edit_cell(path, f"D{rows['a']}", 12)
        edit_cell(path, f"D{rows['summary']}", 32)
        result = service.import_content(path.read_bytes())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(self.quantity("a"), 12)
        self.assertEqual(self.quantity("summary"), 32)
        self.assertNotIn("summary", {p["id"] for p in self.store.list_products()["items"]})
        with self.assertRaises(ValidationError):
            service.prepare(body)


if __name__ == "__main__":
    unittest.main()
