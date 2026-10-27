"""Run the SOP dashboard at http://127.0.0.1:8876 using Python 3.10+."""
from __future__ import annotations

import argparse
import base64
import binascii
import importlib
import json
import logging
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from sop_paths import plan_workbook_path
from sop_store import ConflictError, SOPStore, ValidationError
from sop_workbook import WorkbookSync
from sop_backup import SnapshotBackup
from sop_export import ExportError, MAX_REQUEST_BYTES, PlanExport
from sop_refresh import SalesRefresh

ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("sop")


def make_handler(store, web_root, port, workbook_sync=None, snapshot_backup=None, plan_export=None, source_refresh=None, plan_roundtrip=None):
    plan_export = plan_export or PlanExport(root=ROOT)
    roundtrip_lock = threading.Lock()

    def roundtrip_service():
        nonlocal plan_roundtrip
        with roundtrip_lock:
            if plan_roundtrip is None:
                from sop_roundtrip import PlanRoundtrip
                plan_roundtrip = PlanRoundtrip(store, workbook_sync)
            return plan_roundtrip
    source_refresh = source_refresh or SalesRefresh(store, workbook_sync, root=ROOT,
        reader=lambda **kwargs: importlib.import_module("sop_data").refresh(**kwargs))
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if port == 80:
        allowed_hosts.update({"127.0.0.1", "localhost", "[::1]"})

    class Handler(BaseHTTPRequestHandler):
        server_version = "SOPLocal/1.0"

        def log_message(self, format, *args):
            LOGGER.info("%s %s", self.client_address[0], format % args)

        def _headers(self, code, content_type, length):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()

        def _json(self, body, status=200):
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(encoded))
            if self.command != "HEAD":
                self.wfile.write(encoded)

        def _local_request(self):
            host = self.headers.get("Host", "").lower()
            if host not in allowed_hosts:
                self._json({"error": "仅允许本机访问。"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                if parsed.scheme != "http" or parsed.netloc.lower() != host or parsed.path not in {"", "/"}:
                    self._json({"error": "请求来源不允许。"}, 403)
                    return False
            return True

        def _body(self, max_bytes=8 * 1024 * 1024):
            if self.headers.get_content_type() != "application/json":
                raise ValidationError("请使用 application/json 请求。")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValidationError("请求长度无效。")
            if length <= 0 or length > max_bytes:
                raise ValidationError(f"请求为空或超过 {max_bytes // (1024 * 1024)} MB。")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValidationError("JSON 格式无效。")
            if not isinstance(value, dict):
                raise ValidationError("请求内容必须为对象。")
            return value

        def _handle_error(self, error):
            if isinstance(error, ConflictError):
                self._json({"error": str(error), "conflicts": error.conflicts,
                            **({"source": error.source} if getattr(error, "source", None) else {})}, 409)
            elif isinstance(error, ValidationError):
                self._json({"error": str(error)}, 400)
            elif isinstance(error, ExportError):
                self._json({"error": str(error)}, error.status)
            else:
                LOGGER.exception("Request failed")
                self._json({"error": "操作未完成，原有数据已保留。请查看本地服务日志。"}, 500)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._local_request():
                return
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            try:
                if path == "/api/health":
                    self._json({"app_id": "hima-sop", "workspace": str(ROOT), "port": port})
                elif path == "/api/state":
                    state = store.state()
                    state["source_refresh"] = source_refresh.status()
                    if workbook_sync:
                        state["workbook_sync"] = workbook_sync.status()
                    self._json(state)
                elif path == "/api/products":
                    self._json(store.list_products())
                elif path == "/api/plan-workbook/status" and workbook_sync:
                    self._json({**workbook_sync.status(), "source_refresh": source_refresh.status()})
                elif path == "/api/plan-workbook/download" and workbook_sync:
                    result = workbook_sync.sync(force_export=not workbook_sync.path.is_file())
                    if result["status"] in {"conflict", "error", "pending"}:
                        self._json({"error": result["message"], "workbook_sync": result}, 409)
                        return
                    encoded = workbook_sync.path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", "attachment; filename=SOP-plan-copy.xlsx")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(encoded)
                elif path == "/api/history":
                    self._json(store.history(query.get("product_id"), query.get("month")))
                elif path == "/api/backups/status" and snapshot_backup:
                    self._json(snapshot_backup.status())
                elif path == "/api/snapshots":
                    self._json(snapshot_backup.snapshot_list() if snapshot_backup else store.snapshots())
                elif path.startswith("/api/snapshots/") and path.endswith("/excel") and snapshot_backup:
                    parts = path.strip("/").split("/")
                    if len(parts) != 4:
                        raise ValidationError("快照下载地址无效。")
                    backup = snapshot_backup.ensure(parts[2])
                    if backup["status"] != "ready":
                        self._json({"error": "快照已保留，Excel 备份暂未生成，请稍后重试。", "excel_backup": backup}, 503)
                        return
                    encoded = Path(backup["path"]).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f"attachment; filename=SOP-snapshot-{int(parts[2])}.xlsx")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(encoded)
                elif path.startswith("/api/snapshots/"):
                    snapshot_id = path.rsplit("/", 1)[-1]
                    self._json(snapshot_backup.get_snapshot(snapshot_id) if snapshot_backup else store.get_snapshot(snapshot_id))
                elif path == "/api/compare":
                    self._json(store.compare(query.get("left"), query.get("right")))
                elif path == "/api/matching":
                    self._json(store.matching(query.get("month", ""), query.get("brand", ""), query.get("model", ""),
                                              query.get("product_id", ""), query.get("zero_week_date", "")))
                elif path.startswith("/api/"):
                    self._json({"error": "接口不存在。"}, 404)
                else:
                    relative = "index.html" if path == "/" else path.lstrip("/")
                    target = (web_root / relative).resolve()
                    if not target.is_relative_to(web_root.resolve()) or any(part.startswith(".") for part in Path(relative).parts) or not target.is_file():
                        self._json({"error": "页面不存在。"}, 404)
                        return
                    encoded = target.read_bytes()
                    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                        content_type += "; charset=utf-8"
                    self._headers(200, content_type, len(encoded))
                    if self.command != "HEAD":
                        self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self._handle_error(error)

        def do_POST(self):
            if not self._local_request():
                return
            try:
                path = urlparse(self.path).path
                limit = 17 * 1024 * 1024 if path == "/api/plans/import-xlsx" else MAX_REQUEST_BYTES
                body = self._body(limit)
                if path == "/api/plans/export-xlsx":
                    context = None
                    if "row_product_ids" in body:
                        body, context = roundtrip_service().prepare(body)
                    with plan_export.generate(body, **({"roundtrip": context} if context is not None else {})) as exported:
                        if context is not None:
                            roundtrip_service().register(exported.path, context)
                        encoded = exported.path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f"attachment; filename=SOP-plan-export.xlsx; filename*=UTF-8''{quote(exported.filename, safe='')}")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                elif path == "/api/plans/import-xlsx":
                    if not {"filename", "content_base64"}.issubset(body) or set(body) - {"filename", "content_base64", "choice"}:
                        raise ValidationError("请选择从本看板导出的 Excel 文件。")
                    filename, content = body["filename"], body["content_base64"]
                    if not isinstance(filename, str) or len(filename) > 255 or not filename.lower().endswith(".xlsx"):
                        raise ValidationError("仅支持 .xlsx 格式的计划表。")
                    if not isinstance(content, str) or len(content) > 16 * 1024 * 1024:
                        raise ValidationError("Excel 文件不能超过 12 MB。")
                    if body.get("choice") not in (None, "db", "workbook"):
                        raise ValidationError("请选择保留网页计划或使用表格计划。")
                    try:
                        decoded = base64.b64decode(content, validate=True)
                    except (ValueError, binascii.Error):
                        raise ValidationError("Excel 文件内容无效，请重新选择文件。")
                    if not decoded or len(decoded) > 12 * 1024 * 1024:
                        raise ValidationError("Excel 文件为空或超过 12 MB。")
                    result = roundtrip_service().import_content(decoded, choice=body.get("choice"))
                    if not result.get("ok", False):
                        self._json(result, 409 if result.get("status") == "conflict" else 400)
                        return
                elif path == "/api/plans":
                    result = workbook_sync.save_web_plans(body) if workbook_sync else store.save_plans(body)
                elif path == "/api/products":
                    # Preserve saved Excel edits before changing its row directory.
                    if workbook_sync:
                        with workbook_sync.lock:
                            status = workbook_sync.sync()
                            if status["status"] in {"error", "conflict"} or not workbook_sync._input_checked:
                                self._json({"error": status["message"], "workbook_sync": status}, 409)
                                return
                            result = store.save_products(body)
                            result["workbook_sync"] = workbook_sync.sync()
                    else:
                        result = store.save_products(body)
                elif path in {"/api/plan-workbook/sync", "/api/plan-workbook/export"} and workbook_sync:
                    status = workbook_sync.sync(force_export=path.endswith("/export"), choice=body.get("choice"))
                    result = {"ok": status["status"] not in {"conflict", "error"}, **status}
                elif path == "/api/plan-workbook/open" and workbook_sync:
                    status = workbook_sync.sync(force_export=not workbook_sync.path.is_file())
                    if not workbook_sync.path.is_file():
                        raise ValidationError(status["message"])
                    if os.name != "nt":
                        raise ValidationError("请手动打开计划表：" + str(workbook_sync.path))
                    os.startfile(workbook_sync.path)
                    result = {"ok": True, "path": str(workbook_sync.path), "workbook_sync": status}
                elif path == "/api/shipments":
                    result = store.save_shipments(body)
                elif path == "/api/settings":
                    result = store.update_settings(body)
                    if workbook_sync:
                        result["workbook_sync"] = workbook_sync.sync()
                elif path == "/api/snapshots":
                    if workbook_sync:
                        sync_state = workbook_sync.sync()
                        if sync_state["status"] in {"conflict", "error"} or not getattr(workbook_sync, "_input_checked", True):
                            self._json({"error": sync_state["message"], "workbook_sync": sync_state,
                                        "conflicts": sync_state.get("conflicts", [])},
                                       409 if sync_state["status"] == "conflict" else 400)
                            return
                    result = store.manual_snapshot(body.get("label", ""))
                    if snapshot_backup:
                        result["excel_backup"] = snapshot_backup.ensure(result["snapshot"]["id"])
                elif path == "/api/refresh":
                    result = source_refresh.refresh()
                else:
                    self._json({"error": "接口不存在。"}, 404)
                    return
                self._json(result)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                self._handle_error(error)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="本机 SOP 计划看板")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--web-dir", type=Path, default=ROOT / "web")
    parser.add_argument("--plan-file", type=Path, default=plan_workbook_path(ROOT))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Bind before touching persistent data, so a busy port fails without starting a second writer.
    server = ThreadingHTTPServer((args.host, args.port), BaseHTTPRequestHandler)
    server.daemon_threads = True
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if not (args.data_dir / "sales.json").exists():
        importlib.import_module("sop_data").refresh(output_path=args.data_dir / "sales.json")
    store = SOPStore(args.data_dir / "sop.sqlite3", args.data_dir / "sales.json")
    store.ensure_daily_snapshots()
    workbook_sync = WorkbookSync(store, args.plan_file, auto_create=False)
    workbook_sync.sync()
    source_refresh = SalesRefresh(store, workbook_sync, root=ROOT)
    snapshot_backup = SnapshotBackup(store)
    stop = threading.Event()

    def daily_worker():
        while not stop.wait(8):
            try:
                store.ensure_daily_snapshots()
                workbook_sync.sync()
                source_refresh.poll()
            except Exception:
                LOGGER.exception("Daily snapshot failed; will retry")

    worker = threading.Thread(target=daily_worker, name="daily-snapshot", daemon=True)
    worker.start()

    def backup_worker():
        while not stop.is_set():
            try:
                attempted = snapshot_backup.export_pending(limit=1)
            except Exception:
                LOGGER.exception("Excel snapshot backup failed; will retry")
                attempted = 0
            stop.wait(1 if attempted else 8)

    backup_thread = threading.Thread(target=backup_worker, name="snapshot-excel-backup", daemon=True)
    backup_thread.start()
    server.RequestHandlerClass = make_handler(store, args.web_dir, args.port, workbook_sync, snapshot_backup, source_refresh=source_refresh)
    LOGGER.info("SOP 看板已启动：http://%s:%s", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=2)
        backup_thread.join(timeout=2)
        store.close()


if __name__ == "__main__":
    main()
