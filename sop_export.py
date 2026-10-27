"""Validate and render disposable Excel downloads; never write business records."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sop_runtime import ArtifactDependencyError, ensure_artifact_tool, node_executable
from sop_store import ValidationError, validate_month

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_WORKBOOK_BYTES = 32 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
MAX_MONTHS = 120
MAX_ROWS = 5000
MAX_QUANTITY_CELLS = 300_000
_BAD_XML_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
LOGGER = logging.getLogger("sop.export")


class ExportError(RuntimeError):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ExportFile:
    path: Path
    filename: str


def _text(value, label, limit):
    if not isinstance(value, str) or len(value) > limit or _BAD_XML_TEXT.search(value):
        raise ValidationError(f"{label}必须为有效文本，且不超过 {limit} 字。")
    return value


def _quantity(value):
    if value is not None and (type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER):
        raise ValidationError("导出数量必须为非负安全整数或空值。")
    return value


def validate_export(body):
    fields = {"schema_version", "current_month", "months", "include_locks", "include_versions", "scope", "headers", "rows", "annual_plans"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValidationError("导出请求字段不完整或包含不支持的字段。")
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise ValidationError("导出格式版本不支持。")
    validate_month(body["current_month"])
    if type(body["include_locks"]) is not bool or type(body["include_versions"]) is not bool:
        raise ValidationError("锁单和版本选项必须为布尔值。")
    _text(body["scope"], "筛选范围", 500)
    months = body["months"]
    if not isinstance(months, list) or not 1 <= len(months) <= MAX_MONTHS:
        raise ValidationError(f"请选择 1 至 {MAX_MONTHS} 个导出月份。")
    for month in months:
        validate_month(month)
    if months != sorted(set(months)):
        raise ValidationError("导出月份必须按时间排列，且不可重复。")
    expected_headers = ["层级", "品牌", "车型"] + (["版本"] if body["include_versions"] else []) + ["指标"] + months + ["数据说明"]
    if body["headers"] != expected_headers:
        raise ValidationError("导出表头与所选月份或版本选项不一致。")
    rows, annual = body["rows"], body["annual_plans"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValidationError(f"导出必须包含 1 至 {MAX_ROWS} 行。")
    years = {month[:4] for month in months if month.endswith("-12")}
    if len(rows) * (len(months) + len(years)) > MAX_QUANTITY_CELLS:
        raise ValidationError("导出数量单元格超过 300000 个，请缩小月份或版本范围。")
    if not isinstance(annual, list) or len(annual) != len(rows):
        raise ValidationError("年度计划量必须与导出行一一对应。")
    metric_column = 4 if body["include_versions"] else 3
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(expected_headers):
            raise ValidationError("导出行列数与表头不一致。")
        if row[0] not in ("车型汇总", "版本明细") or (row[0] == "版本明细" and not body["include_versions"]):
            raise ValidationError("导出层级与版本选项不一致。")
        for column in range(1, metric_column):
            _text(row[column], expected_headers[column], 200)
        metric = row[metric_column]
        if metric not in ("锁单数", "SOP计划量") or (metric == "锁单数" and not body["include_locks"]):
            raise ValidationError("导出指标与锁单选项不一致。")
        for value in row[metric_column + 1:-1]:
            _quantity(value)
        _text(row[-1], "数据说明", 10000)
        totals = annual[index]
        if not isinstance(totals, dict) or set(totals) != years:
            raise ValidationError("年度计划年份必须严格对应所选的12月。")
        for value in totals.values():
            _quantity(value)
            if metric == "锁单数" and value is not None:
                raise ValidationError("锁单行不能包含年度SOP计划量。")
    # A detached normalized payload prevents a caller from mutating it during rendering.
    return json.loads(json.dumps(body, ensure_ascii=False, allow_nan=False))


class PlanExport:
    def __init__(self, root=None, writer=None, temp_root=None, timeout=120):
        self.root = Path(root or Path(__file__).resolve().parent).resolve()
        self.writer = writer or self._node_writer
        self.temp_root = Path(temp_root) if temp_root is not None else None
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(2)

    def _node_writer(self, payload):
        node = node_executable()
        script = self.root / "scripts/build_export_workbook.mjs"
        if not node or not script.is_file():
            raise ExportError("Excel导出组件尚未就绪，请稍后重试。")
        try:
            ensure_artifact_tool(self.root, node)
        except ArtifactDependencyError as error:
            raise ExportError(str(error)) from error
        completed = subprocess.run([node, str(script)], input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                                   text=True, encoding="utf-8", capture_output=True, timeout=self.timeout, cwd=self.root,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if completed.returncode:
            LOGGER.error("Excel export writer failed (exit %s): %s", completed.returncode,
                         (completed.stderr or completed.stdout or "no diagnostic output")[-2000:])
            raise ExportError("Excel导出生成失败，请稍后重试。")
        try:
            result = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            LOGGER.error("Excel export writer returned invalid JSON: %s", (completed.stderr or completed.stdout or "empty output")[-2000:])
            raise ExportError("Excel导出组件未返回有效结果。")
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("output_path") != payload["output_path"]:
            LOGGER.error("Excel export writer returned an unsuccessful or mismatched result.")
            raise ExportError("Excel导出组件返回结果不一致。")

    @staticmethod
    def _valid_workbook(path):
        try:
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_WORKBOOK_BYTES or not zipfile.is_zipfile(path):
                return False
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(names) or not any(name.startswith("xl/worksheets/") and name.endswith(".xml") for name in names):
                    return False
                info = archive.getinfo("xl/workbook.xml")
                if info.file_size > 1024 * 1024:
                    return False
                workbook = ET.fromstring(archive.read(info))
                if any(sheet.attrib.get("name", "").lower() in {"_sync", "_meta"} for sheet in workbook.iter()):
                    return False
                return True
        except (OSError, zipfile.BadZipFile, ET.ParseError, RuntimeError):
            return False

    @contextmanager
    def generate(self, body, roundtrip=None):
        payload = validate_export(body)
        if not self.slots.acquire(blocking=False):
            raise ExportError("已有Excel导出正在生成，请稍后再试。")
        try:
            with tempfile.TemporaryDirectory(prefix="sop-export-", dir=self.temp_root) as temporary:
                path = Path(temporary) / "SOP-plan-export.xlsx"
                try:
                    self.writer({**payload, "output_path": str(path),
                                 **({"roundtrip": roundtrip} if roundtrip is not None else {})})
                except subprocess.TimeoutExpired:
                    LOGGER.warning("Excel export exceeded its %s second timeout.", self.timeout)
                    raise ExportError("Excel导出超时，请缩小范围后重试。", 504)
                except ExportError:
                    raise
                except Exception as error:
                    LOGGER.exception("Excel export generation failed.")
                    raise ExportError("Excel导出生成失败，请稍后重试。") from error
                if not self._valid_workbook(path):
                    raise ExportError("Excel导出未生成有效文件，请稍后重试。")
                filename = f"SOP计划_{payload['months'][0]}_{payload['months'][-1]}.xlsx"
                yield ExportFile(path, filename)
        finally:
            self.slots.release()
