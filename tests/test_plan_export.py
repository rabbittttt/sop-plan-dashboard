"""Disposable download validation and HTTP behavior, without a business database."""
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server import make_handler
from sop_export import ExportError, MAX_REQUEST_BYTES, MAX_SAFE_INTEGER, PlanExport, validate_export
from sop_store import ValidationError


def payload():
    return {"schema_version": 1, "current_month": "2026-10", "months": ["2026-12", "2027-01"],
            "include_locks": True, "include_versions": True, "scope": "问界 · M5",
            "headers": ["层级", "品牌", "车型", "版本", "指标", "2026-12", "2027-01", "数据说明"],
            "rows": [["车型汇总", "问界", "M5", "", "锁单数", 200, None, "车型锁单未分摊"],
                     ["车型汇总", "问界", "M5", "", "SOP计划量", 0, None, "缺项不补0"],
                     ["版本明细", "问界", "M5", "=1+1", "SOP计划量", 0, None, "literal label"]],
            "annual_plans": [{"2026": None}, {"2026": 999}, {"2026": 999}]}


def fixture_writer(body):
    """Minimal OOXML fixture for response plumbing; production uses artifact-tool."""
    with zipfile.ZipFile(body["output_path"], "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="SOP计划导出" sheetId="1"/></sheets></workbook>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')


class ExportValidationTest(unittest.TestCase):
    def test_valid_preserves_zero_null_large_totals_and_literal_labels(self):
        body = payload()
        body["rows"][1][5] = MAX_SAFE_INTEGER
        checked = validate_export(body)
        self.assertEqual(checked["rows"][1][5], MAX_SAFE_INTEGER)
        self.assertIsNone(checked["rows"][1][6])
        self.assertEqual(checked["rows"][2][3], "=1+1")
        self.assertEqual(checked["rows"][2][5], 0)
        checked["rows"][1][5] = 5
        self.assertEqual(body["rows"][1][5], MAX_SAFE_INTEGER)

    def test_rejects_unsafe_numeric_types_and_text(self):
        for value in (True, -1, 1.5, float("nan"), float("inf"), MAX_SAFE_INTEGER + 1, "0", {}, []):
            with self.subTest(value=value):
                body = payload()
                body["rows"][1][5] = value
                with self.assertRaises(ValidationError):
                    validate_export(body)
        for value in (None, 123, "bad\x00text", "bad\ud800text", "x" * 10001):
            body = payload()
            body["rows"][1][-1] = value
            with self.assertRaises(ValidationError):
                validate_export(body)

    def test_months_headers_options_and_annual_keys_are_strict(self):
        variants = []
        for key, value in (("schema_version", True), ("current_month", "2026-13"), ("months", []),
                           ("months", ["2027-01", "2026-12"]), ("months", ["2026-12", "2026-12"]),
                           ("include_locks", "true"), ("include_versions", 1), ("scope", "x" * 501)):
            body = payload(); body[key] = value; variants.append(body)
        body = payload(); body["headers"][-1] = "错误"; variants.append(body)
        body = payload(); body["rows"][0].append("extra"); variants.append(body)
        body = payload(); body["rows"][0][0] = "品牌合计"; variants.append(body)
        body = payload(); body["rows"][0][4] = "预测销量"; variants.append(body)
        body = payload(); body["include_locks"] = False; variants.append(body)
        body = payload(); body["annual_plans"] = []; variants.append(body)
        body = payload(); body["annual_plans"][1] = {"2027": 999}; variants.append(body)
        body = payload(); body["annual_plans"][0] = {"2026": 0}; variants.append(body)
        body = payload(); body["output_path"] = "C:/do-not-write.xlsx"; variants.append(body)
        for body in variants:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                validate_export(body)

    def test_row_month_and_cell_count_limits(self):
        body = payload()
        body["rows"] = body["rows"][:1] * 5001
        body["annual_plans"] = [{"2026": None}] * 5001
        with self.assertRaises(ValidationError): validate_export(body)
        body = payload()
        body["months"] = [f"{2020 + i // 12:04d}-{i % 12 + 1:02d}" for i in range(121)]
        with self.assertRaises(ValidationError): validate_export(body)
        body = payload(); body["include_versions"] = False
        body["months"] = [f"{2020 + i // 12:04d}-{i % 12 + 1:02d}" for i in range(120)]
        body["headers"] = ["层级", "品牌", "车型", "指标"] + body["months"] + ["数据说明"]
        body["rows"] = [["车型汇总", "A", "M", "SOP计划量"] + [None] * 120 + [""]] * 2500
        body["annual_plans"] = [{str(y): None for y in range(2020, 2030)}] * 2500
        with self.assertRaises(ValidationError): validate_export(body)


class ExportGenerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_server_chosen_path_is_removed_after_success_and_no_sync_token(self):
        seen = []
        def writer(body):
            seen.append(body)
            fixture_writer(body)
        service = PlanExport(writer=writer, temp_root=self.root)
        with service.generate(payload()) as exported:
            self.assertTrue(exported.path.is_file())
            self.assertTrue(exported.path.is_relative_to(self.root))
            self.assertTrue(exported.filename.endswith(".xlsx"))
        self.assertFalse(exported.path.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(set(seen[0]), set(payload()) | {"output_path"})

    def test_timeout_and_bad_output_are_cleaned_and_have_safe_errors(self):
        for action in ("timeout", "fail", "invalid", "sync"):
            def writer(body):
                path = Path(body["output_path"])
                path.write_bytes(b"partial")
                if action == "timeout": raise subprocess.TimeoutExpired("node", 120)
                if action == "fail": raise RuntimeError("private details")
                if action == "sync":
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("[Content_Types].xml", "<Types/>")
                        archive.writestr("xl/workbook.xml", '<workbook><sheet name="_sync"/></workbook>')
                        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
            service = PlanExport(writer=writer, temp_root=self.root)
            with self.subTest(action=action), self.assertRaises(ExportError) as caught:
                with service.generate(payload()): pass
            self.assertEqual(caught.exception.status, 504 if action == "timeout" else 503)
            self.assertNotIn("private details", str(caught.exception))
            self.assertEqual(list(self.root.iterdir()), [])

    def test_client_disconnect_cleanup_and_capacity_limit(self):
        service = PlanExport(writer=fixture_writer, temp_root=self.root)
        with self.assertRaises(BrokenPipeError):
            with service.generate(payload()): raise BrokenPipeError()
        self.assertEqual(list(self.root.iterdir()), [])
        service.slots.acquire(); service.slots.acquire()
        try:
            with self.assertRaises(ExportError):
                with service.generate(payload()): pass
        finally:
            service.slots.release(); service.slots.release()

    def test_node_uses_stdin_timeout_hidden_window_and_validates_result(self):
        service = PlanExport(root=self.root, timeout=91)
        body = {**payload(), "output_path": str(self.root / "export.xlsx")}
        result = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "output_path": body["output_path"]}), "")
        with patch("sop_export.Path.is_file", return_value=True), patch("sop_export.subprocess.run", return_value=result) as run:
            service._node_writer(body)
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["timeout"], 91)
            self.assertEqual(json.loads(kwargs["input"]), body)
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            result.stdout = "not-json"
            with self.assertRaises(ExportError): service._node_writer(body)


class NoBusinessAccess:
    def __getattr__(self, name):
        raise AssertionError("Export must not access business state: " + name)


class ExportHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = PlanExport(writer=fixture_writer, temp_root=self.root)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.http.RequestHandlerClass = make_handler(NoBusinessAccess(), self.root, self.http.server_port,
                                                    workbook_sync=NoBusinessAccess(), plan_export=self.service)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown(); self.thread.join(); self.http.server_close()
        self.tmp.cleanup()

    def request(self, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        connection.request("POST", "/api/plans/export-xlsx", json.dumps(body if body is not None else payload()),
                           headers or {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_download_is_attachment_without_db_or_sync_access_and_cleanup(self):
        code, headers, data = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        self.assertTrue(data.startswith(b"PK"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_request_origin_types_shape_and_size_rejections_are_json(self):
        for body, headers, expected in [
            ({}, None, 400),
            (payload(), {"Content-Type": "text/plain"}, 400),
            (payload(), {"Content-Type": "application/json", "Origin": "https://evil.example"}, 403),
            (payload(), {"Content-Type": "application/json", "Host": "evil.example"}, 403),
            ({}, {"Content-Type": "application/json", "Content-Length": str(MAX_REQUEST_BYTES + 1)}, 400),
        ]:
            code, response_headers, data = self.request(body, headers)
            self.assertEqual(code, expected)
            self.assertIn("application/json", response_headers["Content-Type"])
            self.assertIn("error", json.loads(data))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_timeout_is_504_json_and_no_temporary_file_survives(self):
        def timeout(body):
            Path(body["output_path"]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired("node", 120)
        self.service.writer = timeout
        code, headers, data = self.request()
        self.assertEqual(code, 504)
        self.assertIn("error", json.loads(data))
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
