"""Catalog API must import pending sheet input before changing its rows."""
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from server import make_handler
from sop_store import SOPStore, TZ, ValidationError


class SheetSync:
    def __init__(self, status="synced", checked=True):
        self.lock = threading.RLock()
        self._input_checked = checked
        self.value = status
        self.calls = 0

    def sync(self):
        self.calls += 1
        return {"status": self.value, "message": "sheet state", "conflicts": []}

    def status(self):
        return {"status": self.value}


class CatalogAPITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        sales = self.root / "sales.json"
        sales.write_text(json.dumps({"meta": {}, "products": [], "sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", sales, lambda: datetime(2026, 10, 23, 12, tzinfo=TZ))
        self.sync = SheetSync()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.http.RequestHandlerClass = make_handler(self.store, self.root, self.http.server_port, self.sync)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method="GET", body=None, path="/api/products"):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port)
        connection.request(method, path, json.dumps(body) if body is not None else None,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def change(self):
        return {"owner": "test", "changes": [{"brand": "A", "model": "Future", "version": "Max"}]}

    def test_create_and_rename_keep_stable_id_and_plan(self):
        code, created = self.request("POST", self.change())
        self.assertEqual(code, 200)
        self.assertEqual(self.sync.calls, 2)
        product = created["items"][0]
        self.store.save_plans({"owner": "test", "changes": [{"product_id": product["id"], "month": "2027-12", "sop": 0, "forecast": None, "revision": 0}]})
        code, changed = self.request("POST", {"owner": "test", "changes": [{"id": product["id"], "revision": product["revision"], "model": "Renamed"}]})
        self.assertEqual(code, 200)
        self.assertEqual(changed["items"][0]["id"], product["id"])
        self.assertEqual(self.request()[1]["items"][0]["model"], "Renamed")
        self.assertEqual(self.store.state()["plans"][0]["sop"], 0)

    def test_unread_sheet_and_conflict_block_catalog_changes(self):
        for status, checked in [("error", True), ("conflict", True), ("pending", False)]:
            self.sync.value, self.sync._input_checked = status, checked
            code, result = self.request("POST", self.change())
            self.assertEqual(code, 409, result)
            self.assertEqual(self.store.list_products()["items"], [])

    def test_locked_export_does_not_reject_saved_catalog(self):
        self.sync.value = "pending"
        code, result = self.request("POST", self.change())
        self.assertEqual(code, 200)
        self.assertEqual(result["workbook_sync"]["status"], "pending")
        self.assertEqual(len(self.store.list_products()["items"]), 1)

    def test_revision_conflict_is_exposed(self):
        _, created = self.request("POST", self.change())
        product = created["items"][0]
        code, _ = self.request("POST", {"changes": [{"id": product["id"], "revision": 0, "model": "Wrong"}]})
        self.assertEqual(code, 409)
        self.assertEqual(self.store.list_products()["items"][0]["model"], "Future")

    def test_rejected_refresh_preserves_published_source(self):
        previous = self.store.sales_path.read_bytes()

        class Reader:
            @staticmethod
            def refresh(output_path):
                candidate = {"meta": {}, "products": [], "sales": [], "model_sales": []}
                output_path.write_text(json.dumps(candidate), encoding="utf-8")
                return candidate

        with patch("server.importlib.import_module", return_value=Reader), patch.object(self.store, "validate_source_data", side_effect=ValidationError("来源冲突")):
            code, _ = self.request("POST", {}, "/api/refresh")
        self.assertEqual(code, 400)
        self.assertEqual(self.store.sales_path.read_bytes(), previous)
        self.assertEqual(list(self.root.glob(".sales-*.pending.json")), [])


if __name__ == "__main__":
    unittest.main()
