"""Readonly generation projections share real plans across Excel view changes."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl

from sop_store import SOPStore, TZ, ValidationError
from sop_workbook import WorkbookSync
from sop_roundtrip import PlanRoundtrip
from sop_xlsx_plan import write_plan_workbook
from sop_xlsx_export import write_export_workbook
from tests import test_inline_renderer as fixtures
from tests.test_workbook_sync import edit_cell


class GenerationExcelTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sop-generation-excel-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "shared_config.py"
        self.config.write_text("MERGE_GROUPS = {'智界 Combo 2026款汇总': ['智界 A 2025款', '智界 B 2026款']}\n", encoding="utf-8")
        rules = patch("sop_source_rules.DEFAULT_CONFIG_PATH", self.config)
        rules.start(); self.addCleanup(rules.stop)
        products = [{"id": pid, "brand": "智界", "model": model, "version": "Max", "generation": generation}
                    for pid, model, generation in [("a", "A", "智界 A 2025款"), ("b", "B", "智界 B 2026款"),
                                                  ("c", "C", "智界 C 2026款"), ("raw", "Combo", "智界 Combo 2026款汇总")]]
        source = self.root / "sales.json"
        source.write_text(json.dumps({"meta": {}, "products": products, "sales": [], "model_sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", source, lambda: datetime(2026, 10, 28, 12, tzinfo=TZ))
        self.addCleanup(self.store.close)
        self.store.save_plans({"owner": "test", "changes": [
            {"product_id": pid, "month": "2026-10", "sop": value, "forecast": None, "revision": 0}
            for pid, value in [("a", 10), ("b", 20), ("c", 3)]]})
        self.path = self.root / "maintenance.xlsx"
        self.sync = WorkbookSync(self.store, self.path, writer=write_plan_workbook)

    def summary(self):
        self.store.update_settings({"generation_mode": "summary"})
        sales = self.store.read_sales()
        self.aggregate = next(p["id"] for p in sales["products"] if p.get("read_only"))
        return sales

    def spec(self):
        return self.sync._parse(self.path.read_bytes())[1]["layout"]["sheets"][0]

    def download(self, name="export", writer=write_export_workbook):
        sales = self.store.read_sales()
        plans = self.store.view_plans(sales)
        lookup = {(p["product_id"], p["month"]): p for p in plans}
        with patch.multiple(fixtures, PRODUCTS=sales["products"], PLANS=plans, LOOKUP=lookup):
            body = fixtures.export_payload(months=["2026-10"], locks=True)
        body["selected_product_ids"] = [p["id"] for p in sales["products"]]
        body["row_product_ids"] = body.pop("roundtrip")["row_product_ids"]
        body["generation_rule_signature"] = sales["generation_view"]["rule_signature"]
        body["generation_mode"] = sales["generation_view"]["mode"]
        service = PlanRoundtrip(self.store)
        clean, context = service.prepare(body)
        path = self.root / (name + ".xlsx")
        writer({**clean, "roundtrip": context, "output_path": str(path)})
        service.register(path, context)
        return service, path, body

    def beta_members(self):
        """Keep model years separate and merge BETA only with its own year."""
        self.config.write_text("MERGE_GROUPS = {'享界 X4 2026款汇总': ['享界 S9 2026款', '享界 S9T 2026款']}\n", encoding="utf-8")
        products = [
            {"id": pid, "brand": "享界", "model": model, "version": "Max", "generation": generation}
            for pid, model, generation in [
                ("s9", "S9", "享界 S9 2024款"),
                ("s9-new", "S9", "享界 S9 2026款"),
                ("s9-beta", "S9 BETA", "享界 S9 BETA 2026款"),
                ("s9t", "S9T", "享界 S9T 2026款"),
                ("raw-x4", "X4", "享界 X4 2026款汇总")]]
        source = self.root / "beta-sales.json"
        source.write_text(json.dumps({"meta": {}, "products": products, "sales": [], "model_sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "beta.sqlite3", source, lambda: datetime(2026, 10, 28, 12, tzinfo=TZ))
        self.addCleanup(self.store.close)
        manual = self.store.save_products({"changes": [{"brand": "享界", "model": "S9", "version": "手工配置"}]})["items"][0]
        self.manual_id = manual["id"]
        dated = self.store.save_products({"changes": [{"brand": "享界", "model": "S9 2026款", "version": "手工年款配置"}]})["items"][0]
        self.dated_manual_id = dated["id"]
        self.store.save_plans({"owner": "test", "changes": [
            {"product_id": pid, "month": "2026-10", "sop": value, "forecast": None, "revision": 0}
            for pid, value in [("s9", 10), ("s9-new", 2), ("s9-beta", 4), ("s9t", 20),
                               (self.manual_id, 6), (self.dated_manual_id, 3)]]})
        self.path = self.root / "beta-maintenance.xlsx"
        self.sync = WorkbookSync(self.store, self.path, writer=write_plan_workbook)

    def test_beta_detail_maintenance_separates_years_and_merges_same_year_labels(self):
        self.beta_members()
        self.assertEqual(self.sync.sync()["status"], "synced")
        spec = self.spec()
        by_id = {p["id"]: p for p in spec["products"]}
        self.assertEqual(set(by_id), {"s9", "s9-new", "s9-beta", "s9t", self.manual_id, self.dated_manual_id})
        self.assertEqual(by_id["s9"]["source_generation"], "享界 S9 2024款")
        self.assertEqual(by_id["s9"]["model"], "S9 2024款")
        self.assertEqual(by_id["s9-new"]["model"], "S9 2026款")
        self.assertEqual(by_id["s9-beta"]["model"], "S9 2026款")
        self.assertEqual(by_id[self.manual_id]["model"], "S9")
        self.assertEqual(by_id[self.dated_manual_id]["model"], "S9 2026款")
        self.assertEqual(by_id["s9-beta"]["source_generation"], "享界 S9 BETA 2026款")
        self.assertFalse(by_id["s9-beta"].get("read_only"))
        rows = {r["product_id"]: r["row"] for r in spec["rows"] if r["kind"] == "product"}
        block = [rows[pid] for pid in ("s9-new", "s9-beta", self.dated_manual_id)]
        book = openpyxl.load_workbook(self.path)
        sheet = book["SOP计划"]
        self.assertIn(f"B{min(block)}:B{max(block)}", set(map(str, sheet.merged_cells.ranges)))
        self.assertEqual(sheet.cell(rows["s9"], 2).value, "S9 2024款")
        self.assertEqual(sheet.cell(min(block), 2).value, "S9 2026款")
        self.assertNotIn(rows["s9"], range(min(block), max(block) + 1))
        for pid, row in rows.items():
            self.assertEqual(sheet.cell(row, spec["product_column"]).value, pid)
        self.assertFalse(sheet[f"M{rows['s9-beta']}"].protection.locked)
        book.close()
        edit_cell(self.path, f"M{rows['s9-beta']}", 9)
        self.assertEqual(self.sync.sync()["status"], "synced")
        self.assertEqual(self.store.db.execute("SELECT sop FROM plans WHERE product_id='s9-beta'").fetchone()[0], 9)
        original = next(p for p in self.store.read_sales(include_excluded=True)["products"] if p["id"] == "s9-beta")
        self.assertEqual(original["model"], "S9 BETA")
        self.assertEqual(original["source_generation"], "享界 S9 BETA 2026款")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 6)

    def test_year_label_is_a_valid_catalog_edit_baseline(self):
        self.beta_members()
        self.assertEqual(self.sync.sync()["status"], "synced")
        spec = self.spec()
        row = next(r["row"] for r in spec["rows"] if r.get("product_id") == "s9")
        edit_cell(self.path, f"B{row}", "S9 Classic 2024款")
        result = self.sync.sync()
        self.assertEqual(result["status"], "synced", result)
        renamed = next(p for p in self.store.read_sales()["products"] if p["id"] == "s9")
        self.assertEqual(renamed["model"], "S9 Classic 2024款")
        self.assertEqual(renamed["source_generation"], "享界 S9 2024款")
        self.assertEqual(self.store.db.execute("SELECT sop FROM plans WHERE product_id='s9'").fetchone()[0], 10)

    def test_summary_exports_configured_year_only_and_old_detail_import_recalculates(self):
        self.beta_members()
        # The prior release exported model labels without a year. Its trusted
        # row identities must remain importable after year-specific display.
        with patch("sop_source_rules._display_labels", side_effect=lambda data: data):
            old_service, old_path, _ = self.download("beta-detail")
        book = openpyxl.load_workbook(old_path)
        old_spec = fixtures.manifest(book)
        rows = {r["product_id"]: r["row"] for r in old_spec["rows"]
                if r["kind"] == "product" and r.get("metric") == "SOP计划量"}
        book.close()
        self.summary()
        summary = self.store.state()
        self.assertEqual({p["model"] for p in summary["products"]}, {"X4 2026款", "S9 2024款", "S9"})
        aggregate_ids = {p["id"] for p in summary["products"] if p.get("read_only")}
        self.assertEqual({pid for p in summary["products"] if p.get("read_only") for pid in p["member_product_ids"]},
                         {"s9-new", "s9-beta", "s9t", self.dated_manual_id})
        self.assertEqual({p["id"] for p in summary["products"] if not p.get("read_only")}, {"s9", self.manual_id})
        self.assertEqual(sum(p["sop"] for p in summary["plans"] if p["product_id"] in aggregate_ids), 29)
        self.assertEqual(sum(p["sop"] for p in summary["plans"]), 45)
        self.assertEqual(self.sync.sync()["status"], "synced")
        self.assertEqual({p["model"] for p in self.spec()["products"]}, {"X4 2026款", "S9 2024款", "S9"})
        service, path, _ = self.download("beta-summary")
        book = openpyxl.load_workbook(path)
        spec = fixtures.manifest(book)
        self.assertEqual({p["model"] for p in spec["products"]}, {"X4 2026款", "S9 2024款", "S9"})
        plan_rows = [r for r in spec["rows"] if r["kind"] == "product" and r.get("metric") == "SOP计划量"]
        self.assertEqual(sum(book["SOP计划"].cell(r["row"], 4).value for r in plan_rows), 45)
        for row in plan_rows:
            self.assertEqual(book["SOP计划"].cell(row["row"], 4).protection.locked,
                             row["product_id"] in aggregate_ids)
            self.assertEqual(book["SOP计划"].cell(row["row"], spec["product_column"]).value, row["product_id"])
        book.close()
        self.assertEqual(service.import_content(path.read_bytes())["imported"], 0)
        edit_cell(old_path, f"D{rows['s9-beta']}", 9)
        edit_cell(old_path, f"D{rows['s9']}", 12)
        edit_cell(old_path, f"D{rows[self.manual_id]}", 7)
        result = old_service.import_content(old_path.read_bytes())
        self.assertEqual(result["imported"], 3)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["catalog_updated"], 0)
        self.assertEqual(sum(p["sop"] for p in self.store.view_plans()), 53)
        self.assertEqual(sum(p["sop"] for p in self.store.view_plans() if p["product_id"] in aggregate_ids), 34)
        original = next(p for p in self.store.read_sales(include_excluded=True)["products"] if p["id"] == "s9")
        self.assertEqual(original["source_generation"], "享界 S9 2024款")
        self.assertEqual(self.store.db.execute("SELECT sop FROM plans WHERE product_id='s9'").fetchone()[0], 12)
        self.assertEqual(self.sync.sync()["status"], "synced")
        spec = self.spec()
        book = openpyxl.load_workbook(self.path, data_only=True)
        self.assertEqual(sum(book["SOP计划"].cell(r["row"], 13).value
                             for r in spec["rows"] if r["kind"] == "product"), 53)
        book.close()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 6)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans WHERE product_id LIKE 'group_%'").fetchone()[0], 0)

    def test_maintenance_switches_views_preserving_pending_detail_edits(self):
        self.assertEqual(self.sync.sync()["status"], "synced")
        first = self.spec()
        row_a = next(r["row"] for r in first["rows"] if r.get("product_id") == "a")
        edit_cell(self.path, f"M{row_a}", 11)
        self.summary()
        self.assertEqual(self.sync.sync()["status"], "synced")
        spec = self.spec()
        row = next(r["row"] for r in spec["rows"] if r.get("product_id") == self.aggregate)
        self.assertEqual(spec["editable_months_by_product"][self.aggregate], [])
        book = openpyxl.load_workbook(self.path, data_only=True)
        self.assertEqual(book["SOP计划"][f"M{row}"].value, 31)
        self.assertTrue(book["SOP计划"][f"M{row}"].protection.locked)
        self.assertTrue(book["SOP计划"][f"C{row}"].protection.locked)
        book.close()
        self.store.update_settings({"generation_mode": "detail"})
        self.assertEqual(self.sync.sync()["status"], "synced")
        self.assertEqual({p["id"] for p in self.spec()["products"]}, {"a", "b", "c"})
        self.assertEqual(self.store.db.execute("SELECT sop FROM plans WHERE product_id='a'").fetchone()[0], 11)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans WHERE product_id LIKE 'group_%'").fetchone()[0], 0)

    def test_export_derives_values_and_rejects_projection_changes(self):
        self.summary()
        service, path, _ = self.download()
        original = path.read_bytes()
        book = openpyxl.load_workbook(path)
        spec = fixtures.manifest(book)
        row = next(r["row"] for r in spec["rows"] if r.get("product_id") == self.aggregate and r.get("metric") == "SOP计划量")
        plain = next(r["row"] for r in spec["rows"] if r.get("product_id") == "c" and r.get("metric") == "SOP计划量")
        self.assertEqual(book["SOP计划"][f"D{row}"].value, 30)
        self.assertTrue(book["SOP计划"][f"D{row}"].protection.locked)
        self.assertTrue(book["SOP计划"][f"C{row}"].protection.locked)
        book.close()
        for field, value in [("D", 999), ("C", "Changed")]:
            with self.subTest(field=field):
                path.write_bytes(original)
                edit_cell(path, f"{field}{row}", value)
                with self.assertRaises(ValidationError):
                    service.import_content(path.read_bytes())
        path.write_bytes(original)
        book = openpyxl.load_workbook(path); book["SOP计划"].delete_rows(row); book.save(path); book.close()
        with self.assertRaisesRegex(ValidationError, "不能删除"):
            service.import_content(path.read_bytes())
        path.write_bytes(original)
        edit_cell(path, f"D{plain}", 8)
        self.store.update_settings({"generation_mode": "detail"})
        self.assertEqual(service.import_content(path.read_bytes())["imported"], 1)
        self.assertEqual(self.store.db.execute("SELECT sop FROM plans WHERE product_id='c'").fetchone()[0], 8)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans WHERE product_id LIKE 'group_%'").fetchone()[0], 0)

    def test_detail_download_imports_after_switch_and_updates_derived_total(self):
        service, path, _ = self.download()
        book = openpyxl.load_workbook(path)
        row = next(r["row"] for r in fixtures.manifest(book)["rows"] if r.get("product_id") == "a" and r.get("metric") == "SOP计划量")
        book.close()
        self.summary()
        edit_cell(path, f"D{row}", 25)
        self.assertEqual(service.import_content(path.read_bytes())["imported"], 1)
        derived = next(r for r in self.store.view_plans() if r["product_id"] == self.aggregate)
        self.assertEqual(derived["sop"], 45)

    def test_changed_rule_rejects_stale_export_and_refreshes_old_readonly_maintenance(self):
        self.summary()
        _, _, body = self.download()
        self.assertEqual(self.sync.sync()["status"], "synced")
        self.config.write_text("MERGE_GROUPS = {'智界 Combo 2026款汇总': ['智界 A 2025款']}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "规则或展示模式已变化"):
            PlanRoundtrip(self.store).prepare(body)
        self.assertEqual(self.sync.sync()["status"], "synced")
        self.assertEqual({p["model"] for p in self.spec()["products"]}, {"Combo 2026款", "B 2026款", "C 2026款"})

    @unittest.skipUnless(fixtures.NODE.is_file(), "Bundled spreadsheet runtime unavailable")
    def test_node_writer_keeps_aggregate_cells_readonly_and_roundtrippable(self):
        self.summary()
        payload, _ = self.sync._context()
        with patch.object(fixtures, "OUT", self.root):
            maintenance = fixtures.generate("build_plan_workbook.mjs",
                {**payload, "token": "node-maintenance", "generated_at": "2026-10-28T12:00:00+08:00"}, "node-maintenance")
        row = next(r["row"] for r in payload["layout"]["sheets"][0]["rows"] if r.get("product_id") == self.aggregate)
        book = openpyxl.load_workbook(maintenance)
        self.assertEqual(book["SOP计划"][f"M{row}"].value, 30)
        self.assertTrue(book["SOP计划"][f"M{row}"].protection.locked)
        self.assertTrue(book["SOP计划"][f"C{row}"].protection.locked)
        book.close()
        def writer(payload):
            with patch.object(fixtures, "OUT", self.root):
                fixtures.generate("build_export_workbook.mjs", payload, "node-export")
        service, path, _ = self.download("node-export", writer)
        book = openpyxl.load_workbook(path)
        row = next(r["row"] for r in fixtures.manifest(book)["rows"] if r.get("product_id") == self.aggregate and r.get("metric") == "SOP计划量")
        self.assertEqual(book["SOP计划"][f"D{row}"].value, 30)
        self.assertTrue(book["SOP计划"][f"C{row}"].protection.locked)
        book.close()
        self.assertEqual(service.import_content(path.read_bytes())["imported"], 0)


if __name__ == "__main__":
    unittest.main()
