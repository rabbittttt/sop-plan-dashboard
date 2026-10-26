"""Immutable SOP snapshot Excel exports, independent of the editable plan workbook."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from sop_runtime import node_executable
from sop_store import ValidationError, stamp


class SnapshotBackup:
    def __init__(self, store, directory=None, writer=None, root=None):
        self.store = store
        self.directory = Path(directory or store.db_path.parent / "backups/sop_excel").resolve()
        self.root = Path(root or Path(__file__).resolve().parent).resolve()
        self.writer = writer or self._node_writer
        self.condition = threading.Condition(threading.RLock())
        self.inflight = set()
        self.errors = {}
        self.retry_at = {}
        self.last_export_at = None

    def _node_writer(self, payload):
        node = node_executable()
        script = self.root / "scripts/build_snapshot_workbook.mjs"
        if not node or not script.is_file():
            raise ValidationError("Excel 备份生成组件尚未就绪，后台将自动重试。")
        completed = subprocess.run([node, str(script)], input=json.dumps(payload, ensure_ascii=False),
                                   text=True, encoding="utf-8", capture_output=True, timeout=120, cwd=self.root,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if completed.returncode:
            details = (completed.stderr or completed.stdout or "未知错误")[-1000:]
            raise ValidationError("Excel 备份生成失败：" + details)

    def path_for(self, snapshot):
        business_date = date.fromisoformat(snapshot["business_date"]).isoformat()
        kind = snapshot["kind"]
        if kind not in {"daily", "sunday", "manual"}:
            raise ValidationError("快照类型无效，无法生成 Excel 备份。")
        snapshot_id = int(snapshot["id"])
        return self.directory / business_date[:7] / f"SOP快照_{business_date}_{kind}_{snapshot_id:06d}.xlsx"

    @staticmethod
    def valid_workbook(path):
        try:
            if not path.is_file() or not zipfile.is_zipfile(path):
                return False
            with zipfile.ZipFile(path) as archive:
                if "xl/workbook.xml" not in archive.namelist():
                    return False
                ET.fromstring(archive.read("xl/workbook.xml"))
                return True
        except (OSError, zipfile.BadZipFile, ET.ParseError, RuntimeError):
            return False

    def info(self, snapshot):
        snapshot_id = int(snapshot["id"])
        path = self.path_for(snapshot)
        with self.condition:
            error = self.errors.get(snapshot_id)
            running = snapshot_id in self.inflight
        if path.is_file() and not self.valid_workbook(path):
            status, message = "error", "已有 Excel 备份文件损坏或不是有效 XLSX，已停止覆盖；请移走损坏文件后重试。"
        elif path.is_file():
            status, message = "ready", "Excel 备份已保存。"
        elif error:
            status, message = "error", error
        else:
            status, message = "pending", "正在生成 Excel 备份。" if running else "Excel 备份待生成。"
        return {"snapshot_id": snapshot_id, "status": status, "path": str(path), "filename": path.name,
                "message": message, "download_url": f"/api/snapshots/{snapshot_id}/excel"}

    def status(self):
        snapshots = self.store.snapshots()["items"]
        items = [self.info(snapshot) for snapshot in snapshots]
        failed = [item for item in items if item["status"] == "error"]
        return {"directory": str(self.directory), "total": len(items),
                "ready": sum(item["status"] == "ready" for item in items),
                "pending": sum(item["status"] == "pending" for item in items),
                "failed": len(failed), "last_error": failed[0]["message"] if failed else None,
                "last_export_at": self.last_export_at, "items": items}

    def snapshot_list(self):
        result = self.store.snapshots()
        for item in result["items"]:
            item["excel_backup"] = self.info(item)
        return result

    def get_snapshot(self, snapshot_id):
        result = self.store.get_snapshot(snapshot_id)
        result["excel_backup"] = self.info(result["snapshot"])
        return result

    def ensure(self, snapshot_id, wait=True, retry=True):
        # get_snapshot copies immutable JSON under a short DB lock; rendering holds no DB lock.
        payload = self.store.get_snapshot(snapshot_id)
        snapshot = payload["snapshot"]
        snapshot_id = int(snapshot["id"])
        target = self.path_for(snapshot)
        with self.condition:
            if target.is_file():
                if self.valid_workbook(target):
                    self.errors.pop(snapshot_id, None)
                return self.info(snapshot)
            if snapshot_id in self.inflight:
                if not wait:
                    return self.info(snapshot)
                deadline = time.monotonic() + 130
                while snapshot_id in self.inflight and time.monotonic() < deadline:
                    self.condition.wait(timeout=min(1, max(0, deadline - time.monotonic())))
                return self.info(snapshot)
            if not retry and self.retry_at.get(snapshot_id, 0) > time.monotonic():
                return self.info(snapshot)
            self.inflight.add(snapshot_id)
            self.errors.pop(snapshot_id, None)
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.pending.xlsx")
            exported_at = stamp(self.store.clock())
            self.writer({"output_path": str(temporary), "snapshot_data": payload, "exported_at": exported_at})
            if not self.valid_workbook(temporary):
                raise ValidationError("Excel 备份未生成有效的 XLSX 文件。")
            try:
                # Both forms publish a complete file atomically and fail if a backup already exists.
                if os.name == "nt":
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
                    temporary.unlink()
                with self.condition:
                    self.last_export_at = exported_at
            except FileExistsError:
                pass
            with self.condition:
                self.errors.pop(snapshot_id, None)
                self.retry_at.pop(snapshot_id, None)
        except Exception as error:
            with self.condition:
                self.errors[snapshot_id] = str(error) or "Excel 备份失败，后台将自动重试。"
                self.retry_at[snapshot_id] = time.monotonic() + 30
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
            with self.condition:
                self.inflight.discard(snapshot_id)
                self.condition.notify_all()
        return self.info(snapshot)

    def export_pending(self, limit=1):
        # Skip cooling-down failures so an old failure cannot starve newer backups.
        snapshots = sorted(self.store.snapshots()["items"], key=lambda item: item["id"])
        attempted = 0
        for snapshot in snapshots:
            if self.path_for(snapshot).is_file():
                continue
            with self.condition:
                if snapshot["id"] in self.inflight or self.retry_at.get(snapshot["id"], 0) > time.monotonic():
                    continue
            self.ensure(snapshot["id"], wait=False, retry=False)
            attempted += 1
            if attempted >= limit:
                break
        return attempted
