"""Watch stable source files and publish validated sales without losing sheet edits."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import importlib
import logging
import os
from pathlib import Path
import threading
import time
import uuid

from sop_paths import sales_source_files
from sop_store import ValidationError

LOGGER = logging.getLogger("sop")


class SalesRefresh:
    def __init__(self, store, workbook_sync=None, *, root=None, reader=None, interval=60, clock=time.monotonic):
        self.store, self.workbook_sync = store, workbook_sync
        self.root = Path(root or Path(__file__).resolve().parent)
        self.reader = reader or (lambda **kwargs: importlib.import_module("sop_data").refresh(**kwargs))
        self.interval, self.clock = interval, clock
        self.lock = threading.RLock()
        self.observed = self.applied = None
        self.next_check = 0
        self._status = {"status": "watching", "message": "自动检查原始文件；保存稳定后读取", "last_refresh": None}

    def status(self):
        with self.lock:
            return dict(self._status)

    def fingerprint(self):
        paths = [self.root / "input_file" / name for name in (
            "鸿蒙智行大定选配比例分析.xlsx", "鸿蒙智行锁单选配比例分析.xlsx")]
        paths = sorted(set(paths) | set(sales_source_files(self.root / "input_file")))
        paths.append(self.root / "data/source_settings.json")
        result = []
        for path in paths:
            try:
                stat = path.stat()
                result.append((str(path), stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                result.append((str(path), None, None))
        return tuple(result)

    def refresh(self):
        # The same lock order as WorkbookSync prevents a source refresh from
        # rebuilding the sheet before saved manual catalog additions are read.
        with self.lock, (self.workbook_sync.lock if self.workbook_sync else nullcontext()):
            if self.workbook_sync:
                sync = self.workbook_sync.sync()
                if sync["status"] in {"error", "conflict"} or not self.workbook_sync._input_checked:
                    raise ValidationError("先处理计划表同步问题后再更新销量：" + sync["message"])
            before = self.fingerprint()
            with self.store.lock:
                self.store.ensure_daily_snapshots()
                pending = self.store.sales_path.with_name(f".sales-{uuid.uuid4().hex}.pending.json")
                previous = self.store.sales_path.read_bytes() if self.store.sales_path.exists() else None
                published = False
                try:
                    candidate = self.reader(output_path=pending)
                    if self.fingerprint() != before:
                        raise ValidationError("原始文件正在更新，稍后将自动重试。")
                    self.store.validate_source_data(candidate)
                    os.replace(pending, self.store.sales_path)
                    published = True
                    data = self.store.record_sales()
                except Exception:
                    if published and previous is not None:
                        pending.write_bytes(previous)
                        os.replace(pending, self.store.sales_path)
                    raise
                finally:
                    pending.unlink(missing_ok=True)
            self.applied = self.observed = before
            self._status = {"status": "synced", "message": "原始文件自动读取已开启", "last_refresh": datetime.now(timezone.utc).isoformat()}
            result = {"ok": True, "meta": data.get("meta", {}), "product_count": len(data["products"]), "sales_count": len(data["sales"])}
            if self.workbook_sync:
                result["workbook_sync"] = self.workbook_sync.sync()
            return result

    def poll(self):
        with self.lock:
            now = self.clock()
            if now < self.next_check:
                return
            self.next_check = now + self.interval
            try:
                current = self.fingerprint()
                if current == self.applied:
                    return
                if current != self.observed:
                    self.observed = current
                    return
                self.refresh()
            except Exception as error:
                self._status.update(status="error", message="原始文件尚未更新，保留上次数据；将自动重试。" + str(error))
                LOGGER.warning("Sales refresh deferred: %s", error)
