"""Catalog edits and month values from one worksheet commit as one change set."""
import copy
import json
import tempfile
import unittest
import sqlite3
from datetime import datetime
from pathlib import Path

from sop_store import ConflictError, SOPStore, TZ, ValidationError


class InlineStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "sales.json"
        self.source.write_text(json.dumps({"meta": {}, "products": [
            {"id": "a", "brand": "问界", "model": "M5", "version": "Max", "generation": "M5-2026"},
            {"id": "b", "brand": "问界", "model": "M5", "version": "Ultra", "generation": "M5-2026"}],
            "sales": []}), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", self.source,
                              lambda: datetime(2026, 10, 26, 12, tzinfo=TZ))
        self.original = {p["id"]: p for p in self.store.list_products()["items"]}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def product(self, pid="a"):
        return next(p for p in self.store.list_products()["items"] if p["id"] == pid)

    def change(self, pid="a", **values):
        return {"id": pid, "baseline": copy.deepcopy(self.original[pid]), **values}

    def save(self, month="2026-10", value=10, pid="a", forecast=44):
        row = self.store.db.execute("SELECT revision FROM plans WHERE product_id=? AND month=?", (pid, month)).fetchone()
        self.store.save_plans({"owner": "网页", "changes": [{"product_id": pid, "month": month,
            "sop": value, "forecast": forecast, "revision": row[0] if row else 0}]})

    def rename_web(self, pid="a", **fields):
        self.store.save_products({"owner": "网页", "changes": [{"id": pid,
            "revision": self.product(pid)["revision"], **fields}]})

    def rows(self, table):
        return [tuple(row) for row in self.store.db.execute("SELECT * FROM " + table + " ORDER BY rowid")]

    def test_rename_keeps_identity_and_source_when_entry_uses_new_name(self):
        self.save()
        result = self.store.import_workbook_plans({
            "catalog_changes": [self.change(model="M5新车型", version="2027款 · 新Max")],
            "entries": [{"brand": "问界", "model": "M5新车型", "version": "新Max",
                         "plans": [{"month": "2026-11", "sop": 0}]}]})
        self.assertEqual(result["catalog_updated_count"], 1)
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["deactivated_count"], 0)
        item = self.product()
        self.assertEqual((item["model"], item["version"], item["id"]), ("M5新车型", "新Max", "a"))
        self.assertTrue(item["source_backed"])
        self.assertEqual(self.store.db.execute("SELECT sop,forecast FROM plans WHERE product_id='a' AND month='2026-10'").fetchone()[:], (10, 44))
        self.store.record_sales()
        self.assertEqual(self.product()["model"], "M5新车型")

    def test_catalog_conflict_rolls_back_other_renames_entries_and_plans(self):
        self.rename_web(version="网页版")
        before = {name: self.rows(name) for name in ("product_catalog", "plans", "events", "catalog_versions")}
        with self.assertRaises(ConflictError) as caught:
            self.store.import_workbook_plans({
                "catalog_changes": [self.change("b", model="B新车型"), self.change(version="Excel版")],
                "entries": [{"brand": "新品牌", "model": "新车型", "version": "新版本", "plans": []}],
                "changes": [{"product_id": "b", "month": "2026-10", "sop": 7, "forecast": None, "revision": 0}]})
        conflict = caught.exception.conflicts[0]
        self.assertEqual((conflict["kind"], conflict["field"], conflict["baseline"], conflict["database"], conflict["workbook"]),
                         ("catalog", "version", "Max", "网页版", "Excel版"))
        for name, rows in before.items():
            self.assertEqual(self.rows(name), rows, name)

    def test_three_way_merge_preserves_unmodified_labels_and_newer_other_fields(self):
        self.rename_web(version="网页版", brand="网页品牌")
        result = self.store.import_workbook_plans({"catalog_changes": [
            self.change(version="Max", model="Excel车型")
        ]})
        self.assertEqual(result["catalog_updated_count"], 1)
        item = self.product()
        self.assertEqual((item["brand"], item["version"], item["model"]), ("网页品牌", "网页版", "Excel车型"))
        no_change = self.store.import_workbook_plans({"catalog_changes": [self.change(version="Max")]})
        self.assertEqual(no_change["catalog_updated_count"], 0)

    def test_choice_applies_only_to_conflicting_fields(self):
        self.rename_web(version="网页版")
        body = {"catalog_changes": [self.change(version="Excel版", model="Excel车型")], "choice": "db"}
        self.store.import_workbook_plans(body)
        self.assertEqual((self.product()["version"], self.product()["model"]), ("网页版", "Excel车型"))
        body["choice"] = "workbook"
        result = self.store.import_workbook_plans(body)
        self.assertEqual(self.product()["version"], "Excel版")
        self.assertEqual(result["catalog_updated_count"], 1)
        self.assertEqual(self.store.import_workbook_plans(body)["catalog_updated_count"], 0)

    def test_deleted_row_deactivates_and_retains_all_business_history(self):
        self.save("2026-08", 7)
        self.save("2027-12", 24)
        before = {name: self.rows(name) for name in ("plans", "shipments", "events", "snapshots")}
        revisions = {row["month"]: row["revision"] for row in self.store.db.execute("SELECT month,revision FROM plans WHERE product_id='a'")}
        body = {"catalog_changes": [self.change(active=False, baseline_plan_revisions=revisions)]}
        result = self.store.import_workbook_plans(body)
        self.assertEqual(result["deactivated_count"], 1)
        self.assertFalse(self.product()["active"])
        self.assertEqual(self.product()["end_month"], "2026-09")
        self.store.record_sales()
        self.assertFalse(self.product()["active"])
        for name, rows in before.items():
            self.assertEqual(self.rows(name), rows, name)
        repeated = self.store.import_workbook_plans(body)
        self.assertEqual((repeated["catalog_updated_count"], repeated["deactivated_count"]), (0, 0))

    def test_delete_sees_new_plan_revisions_even_outside_exported_months(self):
        self.save("2026-10", 9)
        body = {"catalog_changes": [self.change(active=False, baseline_plan_revisions={"2026-10": 1})]}
        self.save("2027-12", 16)
        with self.assertRaises(ConflictError) as caught:
            self.store.import_workbook_plans(body)
        self.assertEqual(caught.exception.conflicts[0]["reason"], "plans_changed")
        self.assertTrue(self.product()["active"])
        kept = self.store.import_workbook_plans({**body, "choice": "db"})
        self.assertEqual(kept["deactivated_count"], 0)
        result = self.store.import_workbook_plans({**body, "choice": "workbook"})
        self.assertEqual(result["deactivated_count"], 1)
        self.assertEqual(self.store.db.execute("SELECT sop FROM plans WHERE product_id='a' AND month='2027-12'").fetchone()[0], 16)

    def test_delete_and_plan_update_or_named_reentry_are_atomic_errors(self):
        for addition in [
            {"changes": [{"product_id": "a", "month": "2026-08", "sop": 20, "forecast": None, "revision": 0}]},
            {"entries": [{"brand": "问界", "model": "M5", "version": "Max", "plans": [{"month": "2026-08", "sop": 20}]}]},
        ]:
            with self.subTest(addition=addition), self.assertRaises(ValidationError):
                self.store.import_workbook_plans({"catalog_changes": [self.change(active=False)], **addition})
            self.assertTrue(self.product()["active"])
            self.assertEqual(self.rows("plans"), [])

    def test_invalid_plan_rolls_back_catalog_edits(self):
        with self.assertRaises(ValidationError):
            self.store.import_workbook_plans({"catalog_changes": [self.change(model="新车型")],
                "changes": [{"product_id": "a", "month": "2026-10", "sop": -1, "forecast": None, "revision": 0}]})
        self.assertEqual(self.product()["model"], "M5")

    def test_duplicate_names_keep_separate_stable_ids(self):
        self.store.import_workbook_plans({"catalog_changes": [self.change("b", version="Max")]})
        products = self.store.list_products()["items"]
        self.assertEqual({p["id"] for p in products}, {"a", "b"})
        self.assertTrue(all(p["name_conflict"] for p in products))
        with self.assertRaises(ValidationError):
            self.store.import_workbook_plans({"entries": [{"brand": "问界", "model": "M5", "version": "Max", "plans": []}]})

    def test_invalid_catalog_baselines_are_rejected(self):
        invalid = [self.change(version=""), self.change(active=0), self.change(active=False, baseline_plan_revisions={"bad": 1}),
                   {"id": "a", "baseline": {}, "version": "新"}, self.change(unexpected="x"),
                   {"id": "missing", "baseline": self.original["a"], "active": False}]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.store.import_workbook_plans({"catalog_changes": [change]})
        with self.assertRaises(ValidationError):
            self.store.import_workbook_plans({"catalog_changes": [self.change(model="M6"), self.change(version="Plus")]})
        self.assertEqual(self.product()["model"], "M5")

    def tracking_table(self):
        self.store.db.execute("CREATE TABLE plan_download_entry_imports(token TEXT PRIMARY KEY,payload TEXT NOT NULL)")

    def test_entry_tracking_merges_resolved_stable_ids_and_is_idempotent(self):
        self.tracking_table()
        token = "a" * 32
        entry = {"brand": "问界", "model": "新车型", "version": "2027款 Max", "plans": [{"month": "2026-10", "sop": 12}]}
        result = self.store.import_workbook_plans({"entry_tracking_token": token, "entries": [entry]})
        self.assertEqual(result["created_count"], 1)
        pid = result["products"][0]["id"]
        tracked = json.loads(self.store.db.execute("SELECT payload FROM plan_download_entry_imports WHERE token=?", (token,)).fetchone()[0])
        self.assertEqual(tracked, {'["问界","新车型","max"]': pid})
        repeated = self.store.import_workbook_plans({"entry_tracking_token": token, "entries": [entry]})
        self.assertEqual(repeated["created_count"], 0)
        self.assertEqual(repeated["products"][0]["id"], pid)
        self.store.import_workbook_plans({"entry_tracking_token": token, "entries": [{"brand": "问界", "model": "M9", "version": "新增", "plans": []}]})
        tracked = json.loads(self.store.db.execute("SELECT payload FROM plan_download_entry_imports WHERE token=?", (token,)).fetchone()[0])
        self.assertEqual(tracked['["问界","新车型","max"]'], pid)
        self.assertEqual(len(tracked), 2)

    def test_tracking_write_failure_rolls_back_new_product_and_plan(self):
        self.tracking_table()
        self.store.db.execute("CREATE TRIGGER deny_tracking BEFORE INSERT ON plan_download_entry_imports BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.import_workbook_plans({"entry_tracking_token": "b" * 32, "entries": [
                {"brand": "问界", "model": "新车型", "version": "新版本", "plans": [{"month": "2026-10", "sop": 20}]}]})
        self.assertEqual(len(self.store.list_products()["items"]), 2)
        self.assertEqual(self.rows("plans"), [])
        self.assertEqual(self.rows("plan_download_entry_imports"), [])

    def test_old_named_entry_cannot_assign_another_id_after_web_rename(self):
        self.tracking_table()
        body = {"entry_tracking_token": "c" * 32, "entries": [
            {"brand": "问界", "model": "新车型", "version": "新版本", "plans": []}]}
        created = self.store.import_workbook_plans(body)["products"][0]
        self.rename_web(created["id"], version="网页更名后")
        with self.assertRaises(ValidationError):
            self.store.import_workbook_plans(body)
        self.assertEqual(len(self.store.list_products()["items"]), 3)
        self.assertEqual(self.product(created["id"])["version"], "网页更名后")


if __name__ == "__main__":
    unittest.main()
