"""Upload/download transport checks; business merge behavior is tested separately."""
import base64
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from server import make_handler
from sop_export import PlanExport
from sop_store import ConflictError
from tests.test_plan_export import NoBusinessAccess, fixture_writer, payload


class RoundtripStub:
    def __init__(self):
        self.uploads = []
        self.registered = []
        self.error = None
        self.result = {"ok": True, "imported": 2, "created": 0, "status": "imported"}

    def prepare(self, body):
        clean = dict(body)
        ids = clean.pop("row_product_ids")
        return clean, {"token": "trusted-token", "row_product_ids": ids}

    def register(self, path, context):
        self.registered.append((path.exists(), context))

    def import_content(self, content, choice=None):
        self.uploads.append((content, choice))
        if self.error:
            raise self.error
        return self.result


class RoundtripHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = RoundtripStub()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.http.RequestHandlerClass = make_handler(
            NoBusinessAccess(), self.root, self.http.server_port,
            workbook_sync=NoBusinessAccess(),
            plan_export=PlanExport(writer=fixture_writer, temp_root=self.root),
            plan_roundtrip=self.service)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()
        self.tmp.cleanup()

    def request(self, body, path="/api/plans/import-xlsx", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        connection.request("POST", path, json.dumps(body), headers or {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def upload(self, **extra):
        return {"filename": "SOP计划.xlsx", "content_base64": base64.b64encode(b"xlsx-content").decode(), **extra}

    def test_download_registered_before_delivery_and_cleaned(self):
        body = {**payload(), "row_product_ids": [None, None, "p1"]}
        code, data = self.request(body, "/api/plans/export-xlsx")
        self.assertEqual(code, 200)
        self.assertTrue(data.startswith(b"PK"))
        self.assertEqual(self.service.registered, [(True, {"token": "trusted-token", "row_product_ids": [None, None, "p1"]})])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_upload_and_conflict_resolution_transport(self):
        code, data = self.request(self.upload())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["imported"], 2)
        self.assertEqual(self.service.uploads, [(b"xlsx-content", None)])
        conflict = ConflictError([{"month": "2026-10", "baseline": 1, "database": 2, "workbook": 3}])
        self.service.error = conflict
        code, data = self.request(self.upload())
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(data)["conflicts"][0]["database"], 2)
        conflict.source = "maintenance"
        code, data = self.request(self.upload(choice="workbook"))
        self.assertEqual(json.loads(data)["source"], "maintenance")
        self.assertEqual(self.service.uploads[-1], (b"xlsx-content", "workbook"))

    def test_invalid_uploads_and_cross_origin_never_reach_service(self):
        for body in [{}, self.upload(filename="bad.xlsm"), self.upload(content_base64="%%%"),
                     self.upload(content_base64=""), self.upload(choice="anything"),
                     self.upload(path="C:/unrelated.xlsx"), self.upload(filename=42)]:
            with self.subTest(body=body):
                code, data = self.request(body)
                self.assertEqual(code, 400)
                self.assertIn("error", json.loads(data))
        code, _ = self.request(self.upload(), headers={"Content-Type": "application/json", "Origin": "https://external.example"})
        self.assertEqual(code, 403)
        self.assertFalse(self.service.uploads)

    def test_returned_conflicts_and_committed_warning_keep_their_status(self):
        self.service.result = {"ok": False, "status": "conflict", "imported": 0,
                               "conflicts": [{"month": "2026-10", "database": 6, "workbook": 8}]}
        code, data = self.request(self.upload())
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(data), self.service.result)
        self.service.result = {"ok": True, "status": "imported", "imported": 2, "created": 0,
                               "warning": "计划已导入；维护表待同步", "workbook_sync": {"status": "pending"}}
        code, data = self.request(self.upload(choice="workbook"))
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data), self.service.result)


if __name__ == "__main__":
    unittest.main()
