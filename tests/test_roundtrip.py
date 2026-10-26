"""Downloaded files merge only edited SOP cells into the unified plan store."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sop_roundtrip import PlanRoundtrip, MAX_UPLOAD_BYTES
from sop_store import ConflictError, SOPStore, TZ, ValidationError
from sop_workbook import WorkbookSync
from tests.test_workbook_sync import NS, col_name, edit_cell, fixture_writer


def export_body(locks=True, versions=True, selected=("a", "b")):
    months = ["2026-10", "2026-12"]
    records = []
    metrics = (["锁单数"] if locks else []) + ["SOP计划量"]
    for metric in metrics:
        records.append((["车型汇总", "A", "M", *([""] if versions else []), metric, 999, 999, ""], None))
    if versions:
        for metric in metrics:
            for pid in selected:
                records.append((["版本明细", "A", "M", "同名版本", metric, None, None, ""], pid))
    return {"schema_version": 1, "current_month": "2026-10", "months": months,
            "include_locks": locks, "include_versions": versions, "scope": "测试",
            "headers": ["层级", "品牌", "车型", *(["版本"] if versions else []), "指标", *months, "数据说明"],
            "rows": [row for row, _ in records], "row_product_ids": [pid for _, pid in records],
            "selected_product_ids": list(selected),
            "annual_plans": [{"2026": None if row[4 if versions else 3] == "锁单数" else 999} for row, _ in records]}


def download_fixture(path, clean, context):
    """Small test-only OOXML standing in for the already-tested artifact writer."""
    grid = [[None] * 6 for _ in range(40)]
    grid[0][0] = "SOP计划"
    grid[3] = ["品牌", "车型", "版本", "10月", "12月", "年度合计"]
    meta = [["SOP_IMPORT", 1], ["token", context["token"]],
            ["include_versions", clean["include_versions"]], ["sheet", "SOP计划"],
            ["kind", "product_id", "month", "cell", "baseline_sop"]]
    metric_col = 4 if clean["include_versions"] else 3
    addresses = {}
    for index, (record, pid) in enumerate(zip(clean["rows"], context["row_product_ids"]), 5):
        metric = record[metric_col]
        grid[index - 1] = [record[1], record[2], record[3] if clean["include_versions"] else metric,
                           *record[metric_col + 1:metric_col + 3], f'=IF(COUNT(D{index}:E{index})=0,"",SUM(D{index}:E{index}))']
        if pid is not None and metric == "SOP计划量":
            for col, month in enumerate(clean["months"], 4):
                if month in context["editable_months_by_product"][pid]:
                    cell = f"{col_name(col)}{index}"
                    meta.append(["plan", pid, month, cell, grid[index - 1][col - 1]])
                    addresses[(pid, month)] = cell
    if clean["include_versions"]:
        for row in range(30, 40):
            for col, field in enumerate(["brand", "model", "version", *clean["months"]], 1):
                if col <= 3 or field in context["manual_editable_months"]:
                    meta.append(["manual", None, field, f"{col_name(col)}{row}", None])
    sheets = [("SOP计划", grid), ("_sop_import", meta)]
    rel = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in (1, 2)) + '</Types>')
        archive.writestr("_rels/.rels", rel + '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        book = ET.Element(f"{{{NS}}}workbook")
        sheet_list = ET.SubElement(book, f"{{{NS}}}sheets")
        for i, (name, rows) in enumerate(sheets, 1):
            ET.SubElement(sheet_list, f"{{{NS}}}sheet", {"name": name, "sheetId": str(i), "state": "veryHidden" if name.startswith("_") else "visible", "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": f"rId{i}"})
            root = ET.Element(f"{{{NS}}}worksheet")
            ET.SubElement(root, f"{{{NS}}}dimension", {"ref": f"A1:{col_name(max(map(len, rows)))}{len(rows)}"})
            data = ET.SubElement(root, f"{{{NS}}}sheetData")
            for r, values in enumerate(rows, 1):
                line = ET.SubElement(data, f"{{{NS}}}row", {"r": str(r)})
                for c, value in enumerate(values, 1):
                    cell = ET.SubElement(line, f"{{{NS}}}c", {"r": f"{col_name(c)}{r}"})
                    if isinstance(value, str) and value.startswith("=IF("):
                        ET.SubElement(cell, f"{{{NS}}}f").text = value[1:]
                    elif isinstance(value, str):
                        cell.set("t", "inlineStr")
                        ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t").text = value
                    elif isinstance(value, bool):
                        cell.set("t", "b")
                        ET.SubElement(cell, f"{{{NS}}}v").text = str(int(value))
                    elif value is not None:
                        ET.SubElement(cell, f"{{{NS}}}v").text = str(value)
            archive.writestr(f"xl/worksheets/sheet{i}.xml", ET.tostring(root, encoding="utf-8"))
        archive.writestr("xl/workbook.xml", ET.tostring(book, encoding="utf-8"))
        archive.writestr("xl/_rels/workbook.xml.rels", rel + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in (1, 2)) + '</Relationships>')
    return addresses


class RoundtripTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / "sales.json"
        source.write_text(json.dumps({"meta": {}, "products": [
            {"id": "a", "brand": "A", "model": "M", "version": "同名版本", "generation": "2026"},
            {"id": "b", "brand": "A", "model": "M", "version": "同名版本", "generation": "2027"}], "sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", source, lambda: datetime(2026, 10, 25, 12, tzinfo=TZ))
        self.service = PlanRoundtrip(self.store)
        self.path = self.root / "export.xlsx"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save(self, pid, month, sop, forecast=None):
        row = self.store.db.execute("SELECT revision FROM plans WHERE product_id=? AND month=?", (pid, month)).fetchone()
        self.store.save_plans({"owner": "网页", "changes": [{"product_id": pid, "month": month, "sop": sop, "forecast": forecast, "revision": row[0] if row else 0}]})

    def value(self, pid="a", month="2026-10", field="sop"):
        row = self.store.db.execute("SELECT * FROM plans WHERE product_id=? AND month=?", (pid, month)).fetchone()
        return row[field] if row else None

    def export(self, **kwargs):
        clean, context = self.service.prepare(export_body(**kwargs))
        self.cells = download_fixture(self.path, clean, context)
        self.service.register(self.path, context)
        self.original = self.path.read_bytes()
        return clean, context

    def change(self, pid, month, value):
        edit_cell(self.path, self.cells[(pid, month)], value)

    def import_file(self, choice=None):
        return self.service.import_content(self.path.read_bytes(), choice)

    def test_authoritative_quantities_partial_scope_and_same_names(self):
        self.save("a", "2026-10", 10)
        self.save("a", "2026-11", 25)
        self.save("b", "2026-10", 20)
        clean, context = self.export(selected=("a",))
        self.assertEqual(clean["rows"][1][5:7], [10, None])
        self.assertEqual(clean["annual_plans"][1], {"2026": 35})
        self.assertEqual(context["annual_known_counts"][1], {"2026": 2})
        self.change("a", "2026-10", 12)
        self.assertEqual(self.import_file()["imported"], 1)
        self.assertEqual(self.value("a"), 12)
        self.assertEqual(self.value("b"), 20)

    def test_web_different_month_and_forecast_are_preserved(self):
        self.save("a", "2026-10", 10, 80)
        self.export()
        self.save("a", "2026-10", 10, 95)
        self.save("a", "2026-12", 30)
        self.save("a", "2027-01", 100)
        self.change("a", "2026-10", 20)
        self.assertEqual(self.import_file()["imported"], 1)
        self.assertEqual(self.value(field="forecast"), 95)
        self.assertEqual(self.value(month="2026-12"), 30)
        self.assertEqual(self.value(month="2027-01"), 100)

    def test_stale_unchanged_copy_never_undoes_prior_import(self):
        self.save("a", "2026-10", 10)
        self.export()
        self.change("a", "2026-10", 20)
        self.assertEqual(self.import_file()["imported"], 1)
        self.assertEqual(self.import_file()["imported"], 0)
        self.assertEqual(self.service.import_content(self.original)["imported"], 0)
        self.assertEqual(self.value(), 20)
        self.change("a", "2026-10", 30)
        self.assertEqual(self.import_file()["status"], "conflict")
        self.assertEqual(self.value(), 20)

    def test_conflict_is_atomic_and_choices_only_resolve_conflicting_cells(self):
        self.save("a", "2026-10", 10)
        self.export()
        self.change("a", "2026-10", 20)
        self.change("b", "2026-10", 30)
        self.save("a", "2026-10", 15)
        result = self.import_file()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflicts"][0]["baseline"], 10)
        self.assertIsNone(self.value("b"))
        self.assertEqual(self.import_file("db")["imported"], 1)
        self.assertEqual(self.value(), 15)
        self.assertEqual(self.value("b"), 30)
        self.assertEqual(self.import_file("workbook")["imported"], 1)
        self.assertEqual(self.value(), 20)

    def test_zero_clear_and_blank_distinction(self):
        self.save("a", "2026-10", 12)
        self.export()
        self.change("a", "2026-10", None)
        self.change("b", "2026-10", 0)
        self.assertEqual(self.import_file()["imported"], 2)
        self.assertIsNone(self.value())
        self.assertEqual(self.value("b"), 0)

    def test_stable_identity_survives_catalog_rename(self):
        self.export()
        product = next(p for p in self.store.list_products()["items"] if p["id"] == "a")
        self.store.save_products({"owner": "test", "changes": [{"id": "a", "brand": "A", "model": "M更新", "version": "版本更新", "revision": product["revision"]}]})
        self.change("a", "2026-10", 9)
        self.assertEqual(self.import_file()["imported"], 1)
        self.assertEqual(self.value(), 9)

    def test_manual_entries_merge_atomically_and_repeat_without_duplicate(self):
        self.export()
        self.change("a", "2026-10", 5)
        for address, value in {"A30": "B", "B30": "N", "C30": "2026款 Max", "D30": 0,
                               "B31": "缺品牌", "C31": "版本", "D31": 8}.items():
            edit_cell(self.path, address, value)
        with self.assertRaises(ValidationError):
            self.import_file()
        self.assertIsNone(self.value())
        self.assertEqual(len(self.store.list_products()["items"]), 2)
        for address in ("B31", "C31", "D31"):
            edit_cell(self.path, address, None)
        result = self.import_file()
        self.assertEqual((result["imported"], result["created"]), (2, 1))
        self.assertEqual(self.import_file()["created"], 0)
        product = next(p for p in self.store.list_products()["items"] if p["model"] == "N")
        self.assertEqual(product["version"], "Max")
        self.assertEqual(self.value(product["id"]), 0)

    def test_same_named_manual_generation_is_rejected(self):
        self.export()
        for address, value in {"A30": "A", "B30": "M", "C30": "同名版本", "D30": 99}.items():
            edit_cell(self.path, address, value)
        with self.assertRaisesRegex(ValidationError, "多个来源代际"):
            self.import_file()
        self.assertIsNone(self.value())

    def test_readonly_formula_and_metadata_tampering_rejected(self):
        self.export()
        for address, value, sheet in (("D5", 7, 1), ("A1", "改标题", 1), ("F5", 7, 1), ("B2", "a" * 32, 2), ("B6", "b", 2)):
            with self.subTest(address=address, sheet=sheet):
                self.path.write_bytes(self.original)
                edit_cell(self.path, address, value, sheet=sheet)
                with self.assertRaises(ValidationError):
                    self.import_file()
        self.path.write_bytes(self.original)
        edit_cell(self.path, self.cells[("a", "2026-10")], "1+1", formula=True)
        with self.assertRaisesRegex(ValidationError, "不能填写公式"):
            self.import_file()

    def test_invalid_request_or_file_has_no_plan_effect(self):
        bad = export_body()
        bad["row_product_ids"][-1] = "a"
        with self.assertRaises(ValidationError):
            self.service.prepare(bad)
        for content in (b"not zip", b"x" * (MAX_UPLOAD_BYTES + 1)):
            with self.assertRaises(ValidationError):
                self.service.import_content(content)
        self.export(versions=False)
        with self.assertRaisesRegex(ValidationError, "只有车型汇总"):
            self.import_file()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM plans").fetchone()[0], 0)

    def test_immutable_baseline_and_lifecycle_locking(self):
        product = next(p for p in self.store.list_products()["items"] if p["id"] == "a")
        self.store.save_products({"owner": "test", "changes": [{"id": "a", "revision": product["revision"], "active": False}]})
        clean, context = self.export()
        self.assertEqual(context["editable_months_by_product"]["a"], [])
        with self.assertRaises(Exception):
            self.store.db.execute("UPDATE plan_download_exports SET payload='{}'")

    def test_primary_saved_edits_are_read_before_export_and_import(self):
        sync = WorkbookSync(self.store, self.root / "primary.xlsx", writer=fixture_writer)
        self.service = PlanRoundtrip(self.store, sync)
        sync.sync()
        context = sync._context()[0]
        cell = next(c["cell"] for c in context["layout"]["cells"] if c["product_id"] == "a" and c["month"] == "2026-10")
        edit_cell(sync.path, cell, 17)
        clean, _ = self.export()
        self.assertEqual(self.value(), 17)
        self.change("a", "2026-10", 25)
        edit_cell(sync.path, cell, 19)
        self.assertEqual(self.import_file()["status"], "conflict")
        self.assertEqual(self.value(), 19)

    def test_missing_primary_workbook_after_sync_does_not_block_export_or_reimport(self):
        sync = WorkbookSync(self.store, self.root / "primary.xlsx", writer=fixture_writer)
        self.service = PlanRoundtrip(self.store, sync)
        self.save("a", "2026-10", 10)
        self.assertEqual(sync.sync()["status"], "synced")
        sync.path.unlink()

        clean, _ = self.export()
        self.assertEqual(next(row[5] for row in clean["rows"] if row[0] == "车型汇总" and row[4] == "SOP计划量"), 10)
        self.assertFalse(sync.path.exists())
        self.change("a", "2026-10", 15)
        result = self.import_file()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.value(), 15)

    def test_missing_primary_workbook_at_start_allows_download_if_rebuild_fails(self):
        sync = WorkbookSync(self.store, self.root / "primary.xlsx", writer=fixture_writer)
        self.service = PlanRoundtrip(self.store, sync)
        self.save("a", "2026-10", 10)
        with patch.object(sync, "writer", side_effect=RuntimeError("primary writer unavailable")):
            clean, _ = self.export()
            self.assertEqual(next(row[5] for row in clean["rows"] if row[0] == "车型汇总" and row[4] == "SOP计划量"), 10)
            self.assertFalse(sync.path.exists())
            self.change("a", "2026-10", 15)
            result = self.import_file()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.value(), 15)

    def test_pending_primary_writeback_reports_committed_success(self):
        sync = WorkbookSync(self.store, self.root / "primary.xlsx", writer=fixture_writer)
        self.service = PlanRoundtrip(self.store, sync)
        self.export()
        self.change("a", "2026-10", 25)
        with patch.object(sync, "writer", side_effect=PermissionError("Excel open")):
            result = self.import_file()
        self.assertTrue(result["ok"])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["workbook_sync"]["status"], "pending")
        self.assertIn("已导入", result["warning"])
        self.assertEqual(self.value(), 25)

    def test_primary_conflict_cannot_be_bypassed_by_download_conflict_choice(self):
        sync = WorkbookSync(self.store, self.root / "primary.xlsx", writer=fixture_writer)
        self.service = PlanRoundtrip(self.store, sync)
        self.export()
        context = sync._context()[0]
        cell = next(c["cell"] for c in context["layout"]["cells"] if c["product_id"] == "a" and c["month"] == "2026-10")
        edit_cell(sync.path, cell, 7)
        self.save("a", "2026-10", 8)
        self.change("b", "2026-10", 20)
        with self.assertRaises(ConflictError) as captured:
            self.import_file("workbook")
        self.assertEqual(captured.exception.source, "maintenance")
        self.assertEqual(self.value(), 8)
        self.assertIsNone(self.value("b"))

    def test_missing_dimensions_are_safe_and_forged_large_coordinates_rejected(self):
        self.export()
        with zipfile.ZipFile(io.BytesIO(self.original)) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        for name in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
            root = ET.fromstring(parts[name])
            root.remove(root.find(f"{{{NS}}}dimension"))
            parts[name] = ET.tostring(root, encoding="utf-8")
        def packed():
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, value in parts.items():
                    archive.writestr(name, value)
            return buffer.getvalue()
        self.assertEqual(self.service.import_content(packed())["imported"], 0)
        root = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        row = ET.SubElement(root.find(f"{{{NS}}}sheetData"), f"{{{NS}}}row", {"r": "400001"})
        ET.SubElement(row, f"{{{NS}}}c", {"r": "A400001"})
        parts["xl/worksheets/sheet1.xml"] = ET.tostring(root, encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "编号"):
            self.service.import_content(packed())

    def test_duplicate_zip_members_and_expansion_limits_rejected(self):
        self.export()
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.original)) as original, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in original.namelist():
                archive.writestr(name, original.read(name))
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("xl/workbook.xml", original.read("xl/workbook.xml"))
        with self.assertRaisesRegex(ValidationError, "重复"):
            self.service.import_content(buffer.getvalue())
        with patch("sop_roundtrip.MAX_EXPANDED_BYTES", 1):
            with self.assertRaisesRegex(ValidationError, "解压后过大"):
                self.import_file()


if __name__ == "__main__":
    unittest.main()
