"""Mode switches sync old input first; polling reads rules afresh."""
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from server import make_handler
from sop_store import SOPStore, TZ
from tests.test_catalog_api import SheetSync


class GenerationAPITest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "shared_config.py"
        self.config.write_text("MERGE_GROUPS = {'A Total': ['A One', 'A Two']}\n", encoding="utf-8")
        rule_patch = patch("sop_source_rules.DEFAULT_CONFIG_PATH", self.config)
        rule_patch.start()
        self.addCleanup(rule_patch.stop)
        source = self.root / "sales.json"
        source.write_text(json.dumps({"meta": {}, "products": [], "sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", source, lambda: datetime(2026, 10, 28, 12, tzinfo=TZ))
        self.addCleanup(self.store.close)
        self.sync = SheetSync()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.http.RequestHandlerClass = make_handler(self.store, self.root, self.http.server_port, self.sync)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()

    def request(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port)
        conn.request("GET" if body is None else "POST", path,
                     json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_switch_syncs_before_and_after_mode_change(self):
        code, result = self.request("/api/settings", {"generation_mode": "summary"})
        self.assertEqual(code, 200, result)
        self.assertEqual(result["settings"]["generation_mode"], "summary")
        self.assertEqual(self.sync.calls, 2)
        self.assertEqual(self.request("/api/state")[1]["generation_view"]["mode"], "summary")

    def test_conflict_does_not_change_mode(self):
        self.sync.value = "conflict"
        code, result = self.request("/api/settings", {"generation_mode": "summary"})
        self.assertEqual(code, 409, result)
        self.assertEqual(self.store.settings().get("generation_mode", "detail"), "detail")

    def test_poll_reloads_rule_change_and_exposes_invalid_config(self):
        path = "/api/plan-workbook/status"
        first = self.request(path)[1]["generation_view"]
        self.config.write_text("MERGE_GROUPS = {'A Total': ['A One', 'A Six']}\n", encoding="utf-8")
        second = self.request(path)[1]["generation_view"]
        self.assertNotEqual(first["rule_signature"], second["rule_signature"])
        self.config.write_text("MERGE_GROUPS = BROKEN\n", encoding="utf-8")
        code, invalid = self.request(path)
        self.assertEqual(code, 200)
        self.assertIn("MERGE_GROUPS", invalid["generation_view"]["error"])
        code, invalid = self.request("/api/settings", {"generation_mode": "summary"})
        self.assertEqual(code, 400, invalid)
        self.assertEqual(self.store.settings().get("generation_mode", "detail"), "detail")
        self.config.write_text("MERGE_GROUPS = {}\n", encoding="utf-8")
        self.assertNotIn("error", self.request(path)[1]["generation_view"])
