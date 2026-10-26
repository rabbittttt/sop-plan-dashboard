"""Legacy schema-4 entry areas remain importable during the inline upgrade."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sop_store import SOPStore, TZ, ValidationError
from sop_workbook import WorkbookSync, _continuous_layout, _signature
from tests.test_workbook_sync import fixture_writer, edit_cell, col_name


class LegacyAreaSync(WorkbookSync):
    """Keep old area-based files for compatibility tests, never production use."""
    def _context(self):
        context, _ = super()._context()
        context.update(schema_version=4, layout_version="continuous-blue-v2")
        plans = {(r["product_id"], r["month"]): r for r in context["plans"]}
        context["layout"] = _continuous_layout(context["products"], context["months"],
            context["editable_months"], context["current_month"], plans, include_manual=True)
        return context, _signature(context)


class ManualCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "sales.json"
        self.data = {"meta": {}, "products": [{"id": "a", "brand": "A", "model": "M", "version": "V1", "generation": "A M 2026"}], "sales": []}
        self.source.write_text(json.dumps(self.data), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", self.source, lambda: datetime(2026, 10, 24, 12, tzinfo=TZ))
        self.path = self.root / "plan.xlsx"
        self.sync = LegacyAreaSync(self.store, self.path, writer=fixture_writer)
        self.assertEqual(self.sync.sync()["status"], "synced")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, row=0, brand="B", model="New", version="2026款 · Max", value=0, month="2027-12"):
        spec = self.sync._parse(self.path.read_bytes())[1]["layout"]["sheets"][0]
        r = spec["manual_section"]["first_row"] + row
        for column, label in zip("ABC", (brand, model, version)):
            edit_cell(self.path, f"{column}{r}", label)
        col = next(c["column"] for c in spec["columns"] if c.get("month") == month)
        address = f"{col_name(col)}{r}"
        edit_cell(self.path, address, value)
        return address

    def products(self):
        return self.store.list_products()["items"]

    def test_named_addition_zero_roundtrip_and_single_source_binding(self):
        self.add()
        self.assertEqual(self.sync.sync()["status"], "synced", self.sync.status())
        manual = next(p for p in self.products() if p["model"] == "New")
        self.assertEqual(manual["version"], "Max")
        self.assertEqual(self.store.state()["plans"][0]["sop"], 0)
        self.assertEqual(self.store.state()["plans"][0]["product_id"], manual["id"])
        self.data["products"].append({"id": "new-source", "brand": "B", "model": "New", "version": "2027款 Max", "generation": "B New 2027"})
        self.store.validate_source_data(self.data)
        self.source.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertEqual(len(self.store.record_sales()["products"]), 2)
        self.assertEqual(self.store.source_aliases(), {"new-source": manual["id"]})
        self.assertEqual(self.store.state()["plans"][0]["sop"], 0)
        self.data["products"] = []
        self.source.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertEqual(len(self.store.record_sales()["products"]), 2)
        self.assertEqual(len(self.store.state()["plans"]), 1)

    def test_invalid_entry_rolls_back_valid_new_entry_and_existing_change(self):
        self.add(row=0)
        self.add(row=1, brand="", model="Incomplete", value=10)
        spec = self.sync._context()[0]
        cell = next(c["cell"] for c in spec["layout"]["cells"] if c["month"] == "2026-10")
        edit_cell(self.path, cell, 77)
        self.assertEqual(self.sync.sync()["status"], "error")
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(self.store.state()["plans"], [])
        with self.assertRaises(ValidationError):
            self.store.import_workbook_plans({"entries": [
                {"brand": "B", "model": "N", "version": "V", "plans": [{"month": "2026-10", "sop": 10}]},
                {"brand": "C", "model": "N", "version": "V", "plans": [{"month": "2026-10", "sop": -1}]}]})
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(self.store.state()["plans"], [])

    def test_unique_existing_name_uses_same_id_and_conflict_is_visible(self):
        self.add(brand="A", model="M", version="V1", value=9, month="2026-10")
        self.store.save_plans({"owner": "test", "changes": [{"product_id": "a", "month": "2026-10", "sop": 8, "forecast": None, "revision": 0}]})
        self.assertEqual(self.sync.sync()["status"], "conflict")
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(self.sync.sync(choice="workbook")["status"], "synced")
        self.assertEqual(self.store.state()["plans"][0]["sop"], 9)
        self.assertEqual(len(self.products()), 1)

    def test_locked_writeback_reimports_edits_and_clear_without_duplicates(self):
        cell = self.add(value=10, month="2026-10")
        with patch.object(self.sync, "writer", side_effect=PermissionError("Excel open")):
            self.assertEqual(self.sync.sync()["status"], "pending")
            edit_cell(self.path, cell, 20)
            self.assertEqual(self.sync.sync()["status"], "pending")
            self.assertEqual(self.store.state()["plans"][0]["sop"], 20)
            edit_cell(self.path, cell, None)
            self.assertEqual(self.sync.sync()["status"], "pending")
            self.assertIsNone(self.store.state()["plans"][0]["sop"])
        self.assertEqual(self.sync.sync()["status"], "synced")
        self.assertEqual(len(self.products()), 2)

    def test_duplicate_generations_remain_separate_and_ambiguous_input_is_rejected(self):
        self.add()
        self.sync.sync()
        self.data["products"] += [{"id": f"new-{i}", "brand": "B", "model": "New", "version": f"{year}款 Max", "generation": f"B New {year}"} for i, year in enumerate([2026, 2027])]
        self.source.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertEqual(len(self.store.record_sales()["products"]), 4)
        self.assertEqual(self.store.source_aliases(), {})
        self.sync.sync()
        self.add(value=100)
        self.assertEqual(self.sync.sync()["status"], "error")
        self.assertEqual(self.store.state()["plans"][0]["sop"], 0)

    def test_schema3_saved_edit_imports_before_upgrade(self):
        context, _ = self.sync._context()
        context.update(schema_version=3, layout_version="continuous-blue-v1")
        context["layout"] = _continuous_layout(context["products"], context["months"], context["editable_months"], context["current_month"], include_manual=False)
        token = str(uuid4())
        self.store.db.execute("INSERT INTO workbook_exports VALUES (?,?,?)", (token, "2026-10-24T12:00:00+08:00", json.dumps(context)))
        fixture_writer({**context, "token": token, "generated_at": "2026-10-24T12:00:00+08:00", "output_path": str(self.path)})
        cell = next(c["cell"] for c in context["layout"]["cells"] if c["month"] == "2026-10")
        edit_cell(self.path, cell, 42)
        self.sync = WorkbookSync(self.store, self.path, writer=fixture_writer)
        self.assertEqual(self.sync.sync()["status"], "synced", self.sync.status())
        self.assertEqual(self.sync._parse(self.path.read_bytes())[1]["schema_version"], 5)
        self.assertEqual(self.store.state()["plans"][0]["sop"], 42)


if __name__ == "__main__":
    unittest.main()
