"""Main-grid catalog edits use the same atomic sync as monthly plan edits."""
import copy
import json
import os
import re
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sop_store import SOPStore, TZ
from sop_workbook import WorkbookSync, _column
from tests.test_workbook_sync import fixture_writer, edit_cell

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def change_rows(path, at, *, insert=None, delete=False, copy_from=None):
    """Simulate a user's whole-row insert/copy/delete in a disposable ZIP fixture."""
    with zipfile.ZipFile(path) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(content["xl/worksheets/sheet1.xml"])
    data = root.find(f"{{{NS}}}sheetData")
    copied = next((copy.deepcopy(row) for row in data if int(row.attrib["r"]) == copy_from), None)
    for row in list(data):
        old = int(row.attrib["r"])
        if delete and old == at:
            data.remove(row)
            continue
        new = old + (1 if not delete and old >= at else -1 if delete and old > at else 0)
        row.set("r", str(new))
        for cell in row:
            cell.set("r", re.sub(r"\d+$", str(new), cell.attrib["r"]))
    if not delete:
        row = copied if copied is not None else ET.Element(f"{{{NS}}}row")
        row.set("r", str(at))
        for cell in row:
            cell.set("r", re.sub(r"\d+$", str(at), cell.attrib["r"]))
        if insert:
            for column, value in insert.items():
                cell = ET.SubElement(row, f"{{{NS}}}c", {"r": f"{column}{at}"})
                if isinstance(value, str):
                    cell.set("t", "inlineStr")
                    ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = value
                elif value is not None:
                    ET.SubElement(cell, f"{{{NS}}}v").text = str(value)
        data.append(row)
    data[:] = sorted(data, key=lambda r: int(r.attrib["r"]))
    content["xl/worksheets/sheet1.xml"] = ET.tostring(root, encoding="utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in content.items():
            archive.writestr(name, raw)


class InlineWorkbookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "sales.json"
        self.source.write_text(json.dumps({"meta": {}, "products": [
            {"id": "a", "brand": "问界", "model": "M5", "version": "Max"},
            {"id": "b", "brand": "问界", "model": "M5", "version": "Ultra"}], "sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", self.source, lambda: datetime(2026, 10, 26, 12, tzinfo=TZ))
        self.addCleanup(self.store.close)
        self.path = self.root / "plan.xlsx"
        self.sync = WorkbookSync(self.store, self.path, writer=fixture_writer)
        self.assertEqual(self.sync.sync()["status"], "synced")

    def spec(self):
        return self.sync._parse(self.path.read_bytes())[1]["layout"]["sheets"][0]

    def row(self, pid):
        return next(r["row"] for r in self.spec()["rows"] if r.get("product_id") == pid)

    def month_col(self, month):
        return _column(next(c["column"] for c in self.spec()["columns"] if c.get("month") == month))

    def plan(self, pid, month="2026-10"):
        row = self.store.db.execute("SELECT * FROM plans WHERE product_id=? AND month=?", (pid, month)).fetchone()
        return dict(row) if row else None

    def products(self):
        return {p["id"]: p for p in self.store.list_products()["items"]}

    def test_inline_add_rename_and_zero_share_one_directory(self):
        self.assertIsNone(self.spec()["manual_section"])
        col = self.month_col("2026-10")
        edit_cell(self.path, f"C{self.row('a')}", "2027款 新Max")
        change_rows(self.path, self.row("b"), insert={"A": "问界", "B": "M7", "C": "2027款 新增版", col: 0})
        status = self.sync.sync()
        self.assertEqual(status["status"], "synced", status)
        self.assertEqual(self.products()["a"]["version"], "新Max")
        added = next(p for p in self.products().values() if p["model"] == "M7")
        self.assertEqual(added["version"], "新增版")
        self.assertEqual(self.plan(added["id"])["sop"], 0)
        self.assertEqual(len(self.products()), 3)

    def test_delete_hides_deactivated_row_but_keeps_plan_and_source(self):
        self.store.save_plans({"owner": "验收", "changes": [{"product_id": "a", "month": "2026-10", "sop": 12, "forecast": 20, "revision": 0}]})
        self.sync.sync()
        before = self.plan("a")
        change_rows(self.path, self.row("a"), delete=True)
        status = self.sync.sync()
        self.assertEqual(status["status"], "synced", status)
        self.assertFalse(self.products()["a"]["active"])
        self.assertEqual(self.plan("a"), before)
        self.assertTrue(next(r for r in self.spec()["rows"] if r.get("product_id") == "a")["hidden"])
        self.store.record_sales()
        self.assertFalse(self.products()["a"]["active"])

    def test_merged_labels_reimport_and_deleting_anchor_keeps_other_version(self):
        first, second = self.row("a"), self.row("b")
        with zipfile.ZipFile(self.path) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        merges = {area.attrib["ref"] for area in sheet.findall(f".//{{{NS}}}mergeCell")}
        brand_total = next(row["row"] for row in self.spec()["rows"] if row["kind"] == "brand_total")
        self.assertIn(f"A{first}:A{brand_total}", merges)
        self.assertIn(f"B{first}:B{second}", merges)
        self.assertIsNone(next(row for row in sheet.findall(f".//{{{NS}}}row")
                               if row.attrib["r"] == str(second)).find(f"{{{NS}}}c[@r='A{second}']/{{{NS}}}is"))
        edit_cell(self.path, f"{self.month_col('2026-10')}{second}", 17)
        self.assertEqual(self.sync.sync()["status"], "synced")
        self.assertEqual(self.plan("b")["sop"], 17)
        change_rows(self.path, self.row("a"), delete=True)
        result = self.sync.sync()
        self.assertEqual(result["status"], "synced", result)
        self.assertFalse(self.products()["a"]["active"])
        self.assertTrue(self.products()["b"]["active"])
        self.assertEqual(self.plan("b")["sop"], 17)

    def test_new_row_in_actual_merged_brand_block_inherits_brand(self):
        row = self.row("b")
        change_rows(self.path, row, insert={"B": "M7", "C": "新增版本", self.month_col("2026-10"): 8})
        result = self.sync.sync()
        self.assertEqual(result["status"], "synced", result)
        added = next(p for p in self.products().values() if p["model"] == "M7")
        self.assertEqual(added["brand"], "问界")
        self.assertEqual(self.plan(added["id"])["sop"], 8)

    def test_new_row_outside_merged_block_without_brand_model_is_rejected(self):
        row = self.spec()["last_row"] + 1
        change_rows(self.path, row, insert={"C": "新增版本", self.month_col("2026-10"): 8})
        result = self.sync.sync()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(len(self.products()), 2)

    def test_previous_unmerged_layout_can_still_import(self):
        ledger = self.store.db.execute("SELECT token, created_at, payload FROM workbook_exports").fetchone()
        old = json.loads(ledger["payload"])
        old["layout_version"] = "continuous-inline-v1"
        spec = old["layout"]["sheets"][0]
        spec["merges"] = [area for area in spec["merges"]
                          if not ((match := re.fullmatch(r"([AB])(\d+):\1(\d+)", area))
                                  and int(match.group(2)) >= 5)]
        for item in spec["rows"]:
            if item["kind"] == "product":
                item["label_a"] = item["brand"]
                item["label_b"] = item["model"]
                item["labels"] = [item["brand"], item["model"], item["label_c"]]
        token = uuid4().hex
        self.store.db.execute("INSERT INTO workbook_exports (token, created_at, payload) VALUES (?,?,?)",
                              (token, ledger["created_at"], json.dumps(old, ensure_ascii=False)))
        fixture_writer({**old, "token": token, "generated_at": "legacy test",
                        "output_path": str(self.path)})
        edit_cell(self.path, f"{self.month_col('2026-10')}{self.row('b')}", 23)
        result = self.sync.sync()
        self.assertEqual(result["status"], "synced", result)
        self.assertEqual(self.plan("b")["sop"], 23)

    def test_copy_row_then_change_name_creates_new_id(self):
        row = self.row("a")
        change_rows(self.path, row + 1, copy_from=row)
        edit_cell(self.path, f"C{row+1}", "全新版本")
        status = self.sync.sync()
        self.assertEqual(status["status"], "synced", status)
        self.assertEqual(self.products()["a"]["version"], "Max")
        self.assertEqual(len(self.products()), 3)

    def test_ambiguous_copied_row_rolls_back_all_edits(self):
        row = self.row("a")
        change_rows(self.path, row + 1, copy_from=row)
        edit_cell(self.path, f"C{row}", "名字一")
        edit_cell(self.path, f"C{row+1}", "名字二")
        self.assertEqual(self.sync.sync()["status"], "error")
        self.assertEqual(self.products()["a"]["version"], "Max")
        self.assertEqual(len(self.products()), 2)

    def test_excel_open_sequential_renames_can_restore_original_name(self):
        address = f"C{self.row('a')}"
        with patch("sop_workbook.os.replace", side_effect=PermissionError("open in Excel")):
            edit_cell(self.path, address, "第一次")
            self.assertEqual(self.sync.sync()["status"], "pending")
            edit_cell(self.path, address, "第二次")
            self.assertEqual(self.sync.sync()["status"], "pending")
            edit_cell(self.path, address, "Max")
            self.assertEqual(self.sync.sync()["status"], "pending")
        self.assertEqual(self.products()["a"]["version"], "Max")
        self.assertEqual(self.sync.sync()["status"], "synced")

    def test_stale_deleted_row_conflicts_with_new_web_plan(self):
        row = self.row("a")
        self.store.save_plans({"owner": "网页", "changes": [{"product_id": "a", "month": "2027-10", "sop": 31, "forecast": None, "revision": 0}]})
        change_rows(self.path, row, delete=True)
        state = self.sync.sync()
        self.assertEqual(state["status"], "conflict", state)
        self.assertTrue(self.products()["a"]["active"])
        self.assertEqual(self.sync.sync(choice="db")["status"], "synced")
        self.assertTrue(self.products()["a"]["active"])
        self.assertEqual(self.plan("a", "2027-10")["sop"], 31)

    def test_open_workbook_rename_then_delete_keeps_accepted_name(self):
        row = self.row("a")
        with patch("sop_workbook.os.replace", side_effect=PermissionError("open in Excel")):
            edit_cell(self.path, f"C{row}", "已经改好的名称")
            self.assertEqual(self.sync.sync()["status"], "pending")
            change_rows(self.path, row, delete=True)
            status = self.sync.sync()
            self.assertEqual(status["status"], "pending", status)
        product = self.products()["a"]
        self.assertFalse(product["active"])
        self.assertEqual(product["version"], "已经改好的名称")
        self.assertEqual(self.sync.sync()["status"], "synced")

    def test_new_row_waits_for_excel_close_before_registering_final_name(self):
        row = self.row("b")
        col = self.month_col("2026-10")
        change_rows(self.path, row, insert={"A": "问界", "B": "M7", "C": "暂定名称", col: 7})
        with patch.object(self.sync, "_structural_file_ready", return_value=False):
            state = self.sync.sync()
            self.assertEqual(state["status"], "pending", state)
            self.assertFalse(self.sync._input_checked)
            self.assertEqual(len(self.products()), 2)
            edit_cell(self.path, f"C{row}", "最终名称")
            self.assertEqual(self.sync.sync()["status"], "pending")
            self.assertEqual(len(self.products()), 2)
        state = self.sync.sync()
        self.assertEqual(state["status"], "synced", state)
        added = next(p for p in self.products().values() if p["model"] == "M7")
        self.assertEqual(added["version"], "最终名称")
        self.assertEqual(self.plan(added["id"])["sop"], 7)
        self.assertEqual(len(self.products()), 3)

    def test_late_writeback_lock_cannot_create_duplicate_after_new_row_rename(self):
        row = self.row("b")
        change_rows(self.path, row, insert={"A": "问界", "B": "M7", "C": "已登记新版本"})
        with patch("sop_workbook.os.replace", side_effect=PermissionError("opened after preflight")):
            self.assertEqual(self.sync.sync()["status"], "pending")
        edit_cell(self.path, f"C{row}", "再次更名")
        state = self.sync.sync()
        self.assertEqual(state["status"], "error", state)
        self.assertIn("保留这些已登记名称", state["message"])
        self.assertEqual(len(self.products()), 3)
        edit_cell(self.path, f"C{row}", "已登记新版本")
        self.assertEqual(self.sync.sync()["status"], "synced")

    @unittest.skipUnless(os.name == "nt", "Windows sharing probe")
    def test_windows_excel_style_sharing_blocks_structural_probe(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        handle = kernel.CreateFileW(str(self.path), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            self.assertFalse(self.sync._structural_file_ready())
        finally:
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle(handle)
        self.assertTrue(self.sync._structural_file_ready())


if __name__ == "__main__":
    unittest.main()
