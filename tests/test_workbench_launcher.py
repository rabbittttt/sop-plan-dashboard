"""Exercise the actual Windows console launcher in disposable workspaces."""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_CLOSE = r'''
import ctypes, sys
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
user = ctypes.WinDLL('user32', use_last_error=True)
kernel.AttachConsole.argtypes = [wintypes.DWORD]
kernel.GetConsoleWindow.restype = wintypes.HWND
user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel.FreeConsole()
if not kernel.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
window = kernel.GetConsoleWindow()
kernel.FreeConsole()
if not window or not user.PostMessageW(window, 0x0010, 0, 0):
    raise ctypes.WinError(ctypes.get_last_error())
'''


@unittest.skipUnless(os.name == "nt", "Windows console lifecycle")
class WorkbenchLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sop-launcher-")
        self.root = Path(self.temp.name).resolve() / "计划工作台 测试"
        self.root.mkdir()
        for source in ROOT.glob("*.py"):
            shutil.copy2(source, self.root / source.name)
        for name in ("启动工作台.cmd", "start-sop.ps1"):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / "web").mkdir()
        (self.root / "web/index.html").write_text("<title>SOP launcher test</title>", encoding="utf-8")
        (self.root / "input_file").mkdir()
        (self.root / "data").mkdir()
        (self.root / "data/sales.json").write_text(json.dumps({"meta": {}, "products": [
            {"id": "launcher-test", "brand": "智界", "model": "测试 2026款", "version": "Max"}],
            "sales": []}), encoding="utf-8")
        rules = Path(self.temp.name) / "订单分析/scripts/config/shared_config.py"
        rules.parent.mkdir(parents=True)
        rules.write_text("MERGE_GROUPS = {}\n", encoding="utf-8")
        self.env = {**os.environ, "SOP_PYTHON_PATH": sys.executable, "SOP_EXCEL_ENGINE": "python",
                    "PYTHONIOENCODING": "utf-8"}
        self.processes, self.handles = [], []
        self.port = self.free_port()

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                process.wait(timeout=15)
        for handle in self.handles:
            handle.close()
        self.temp.cleanup()

    @staticmethod
    def free_port():
        for port in range(9100, 9900):
            with socket.socket() as listener:
                try:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    listener.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("No free test port")

    def request(self, path="/api/health", body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=1)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body) if body is not None else None,
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 200, result)
            return result
        finally:
            connection.close()

    def wait_ready(self, process, mode="console"):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                health = self.request()
                if health.get("launch_mode") == mode:
                    self.assertEqual(health["workspace"], str(self.root))
                    return health
            except (OSError, ValueError):
                pass
            if process.poll() is not None:
                self.fail("Launcher exited: " + (self.root / "launch.log").read_text(encoding="utf-8", errors="replace"))
            time.sleep(.1)
        self.fail("Workbench did not become ready")

    def launch(self):
        output = (self.root / "launch.log").open("ab")
        self.handles.append(output)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        # An actual (hidden for testing) console is necessary to exercise X-close.
        process = subprocess.Popen(["cmd.exe", "/d", "/c", "启动工作台.cmd", "-NoBrowser", "-Port", str(self.port)],
                                   cwd=self.root, env=self.env, stdout=output, stderr=output,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        self.processes.append(process)
        return process

    def run_duplicate(self):
        return subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(self.root / "start-sop.ps1"), "-NoBrowser", "-Port", str(self.port)],
                              cwd=self.root, env=self.env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)

    def close_window(self, process):
        result = subprocess.run([sys.executable, "-c", CONSOLE_CLOSE, str(process.pid)],
                                capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        process.wait(timeout=15)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.request()
            except OSError:
                return
            time.sleep(.1)
        self.fail("Closing the launcher console left a listening service")

    def test_close_console_releases_service_and_keeps_saved_plan(self):
        process = self.launch()
        health = self.wait_ready(process)
        month = datetime.now().strftime("%Y-%m")
        self.request("/api/plans", {"owner": "启动测试", "changes": [{
            "product_id": "launcher-test", "month": month, "sop": 17, "forecast": None, "revision": 0}]})
        duplicate = self.run_duplicate()
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertIn("已在另一个窗口运行", duplicate.stdout)
        self.assertEqual(self.request()["pid"], health["pid"])
        self.close_window(process)
        with closing(sqlite3.connect(self.root / "data/sop.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT sop FROM plans WHERE product_id='launcher-test' AND month=?", (month,)).fetchone()[0], 17)
        self.assertFalse((self.root / "input_file/SOP计划维护.xlsx").exists())
        restarted = self.launch()
        self.assertNotEqual(self.wait_ready(restarted)["pid"], health["pid"])
        self.close_window(restarted)

    def test_foreign_service_is_left_running_without_touching_data(self):
        class ForeignHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"app_id":"another-app"}')

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", self.port), ForeignHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                result = self.run_duplicate()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("端口", result.stdout)
                self.assertEqual(self.request()["app_id"], "another-app")
                self.assertFalse((self.root / "data/sop.sqlite3").exists())
            finally:
                server.shutdown()
                worker.join(timeout=5)

    def test_legacy_handover_checks_identity_before_stopping_process(self):
        source = self.root / "server.py"
        current = source.read_text(encoding="utf-8")
        source.write_text(current.replace('launch_mode = "console"', 'launch_mode = None'), encoding="utf-8")
        output = (self.root / "launch.log").open("ab")
        self.handles.append(output)
        legacy = subprocess.Popen([sys.executable, "-u", str(source), "--port", str(self.port)],
                                  env=self.env, cwd=self.root, stdout=output, stderr=output,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        started = datetime.now(timezone.utc).isoformat()
        self.processes.append(legacy)
        self.wait_ready(legacy, mode=None)
        source.write_text(current, encoding="utf-8")
        metadata = {"pid": legacy.pid, "started_at": "2000-01-01T00:00:00Z", "workspace": str(self.root),
                    "executable": sys.executable, "port": self.port}
        record = self.root / "data/service.json"
        record.write_text(json.dumps(metadata), encoding="utf-8")
        rejected = self.run_duplicate()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIsNone(legacy.poll(), "A stale process identity must never be killed")
        self.assertEqual(self.request()["pid"], legacy.pid)
        metadata["started_at"] = started
        record.write_text(json.dumps(metadata), encoding="utf-8")
        (self.root / "data/service.pid").write_text(str(legacy.pid), encoding="ascii")
        workbench = self.launch()
        health = self.wait_ready(workbench)
        self.assertNotEqual(health["pid"], legacy.pid)
        legacy.wait(timeout=5)
        self.assertFalse(record.exists())
        self.assertFalse((self.root / "data/service.pid").exists())
        self.close_window(workbench)


if __name__ == "__main__":
    unittest.main()
