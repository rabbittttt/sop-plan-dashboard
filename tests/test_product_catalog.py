import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sop_store import ConflictError, SOPStore, TZ, ValidationError


class ProductCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "sales.json"
        self.current = datetime(2026, 10, 23, 12, tzinfo=TZ)
        self.source = {"meta": {}, "products": [
            {"id": "stable-2026", "brand": "问界", "model": "M5", "version": "2026款 · Max",
             "generation": "问界 M5 2026款", "configuration": "Max", "source_backed": True}],
            "sales": [{"product_id": "stable-2026", "month": "2026-09", "metric": "locked_orders", "value": 0}], "sources": []}
        self.write_source()
        self.store = SOPStore(self.root / "catalog.sqlite3", self.path, lambda: self.current)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def write_source(self):
        self.path.write_text(json.dumps(self.source, ensure_ascii=False), encoding="utf-8")

    def product(self, pid="stable-2026"):
        return next(p for p in self.store.list_products()["items"] if p["id"] == pid)

    def update(self, pid="stable-2026", **values):
        return self.store.save_products({"owner": "测试维护人", "changes": [
            {"id": pid, "revision": self.product(pid)["revision"], **values}]})["items"][0]

    def plan(self, pid="stable-2026", month="2026-10", sop=10, revision=0):
        return self.store.save_plans({"owner": "测试维护人", "changes": [
            {"product_id": pid, "month": month, "sop": sop, "forecast": 20, "revision": revision}]})

    def test_year_prefix_is_display_only_and_manual_future_product_has_no_fake_sales(self):
        self.assertEqual(self.product()["version"], "Max")
        self.assertEqual(self.product()["source_generation"], "问界 M5 2026款")
        created = self.store.save_products({"changes": [{"brand": "新品牌", "model": "未来车型", "version": "旗舰版"}]})["items"][0]
        self.assertTrue(created["id"].startswith("manual_"))
        self.assertFalse(created["source_backed"])
        self.assertTrue(created["active"])
        self.assertEqual(len(self.store.state()["sales"]), 1)
        self.plan(created["id"], "2027-01", 0)
        self.assertEqual(self.store.state()["plans"][0]["sop"], 0)

    def test_refresh_keeps_absent_products_and_manual_names_without_merging_generations(self):
        self.plan()
        self.update(model="新M5", version="Max首代")
        self.source["products"] = []
        self.source["sales"] = []
        self.write_source()
        state = self.store.record_sales()
        self.assertEqual(len(state["products"]), 1)
        self.assertFalse(state["products"][0]["source_present"])
        self.assertTrue(state["products"][0]["active"])
        self.assertEqual(self.store.state()["plans"][0]["product_id"], "stable-2026")
        self.source["products"] = [
            {"id": "stable-2026", "brand": "问界", "model": "来源新名", "version": "2026款 · Ultra", "generation": "问界 M5 2026款"},
            {"id": "stable-2027", "brand": "问界", "model": "新M5", "version": "2027款 · Max首代", "generation": "问界 M5 2027款"}]
        self.write_source()
        products = self.store.record_sales()["products"]
        self.assertEqual(len(products), 2)
        self.assertTrue(all(p["name_conflict"] for p in products))
        self.assertEqual(self.product()["version"], "Max首代")
        self.assertEqual(self.product()["model"], "新M5")
        self.assertTrue(self.product()["source_present"])
        self.assertEqual({p["id"] for p in products}, {"stable-2026", "stable-2027"})

    def test_unique_manual_and_new_source_same_label_share_one_identity(self):
        manual = self.store.save_products({"changes": [{"brand": "问界", "model": "M7", "version": "Max"}]})["items"][0]
        self.source["products"].append({"id": "source-m7", "brand": "问界", "model": "M7", "version": "2027款 · Max"})
        self.write_source()
        self.store.record_sales()
        self.assertFalse(self.product(manual["id"])["name_conflict"])
        self.assertEqual(self.store.source_aliases(), {"source-m7": manual["id"]})
        self.assertEqual(len(self.store.list_products()["items"]), 2)
        self.update(manual["id"], version="Max筹备版")
        self.assertFalse(self.product(manual["id"])["name_conflict"])

    def test_deactivation_retains_future_plans_and_allows_historical_correction(self):
        self.plan(month="2027-01", sop=123)
        self.update(active=False)
        self.assertEqual(self.store.state()["plans"][0]["sop"], 123)
        for month in ("2026-10", "2027-02"):
            with self.assertRaises(ValidationError):
                self.plan(month=month)
        self.plan(month="2026-09", sop=0)
        self.assertFalse(self.product()["active"])
        self.store.record_sales()
        self.assertFalse(self.product()["active"])
        self.update(active=True)
        self.plan(month="2027-01", sop=124, revision=1)

    def test_catalog_updates_are_atomic_concurrent_and_persistent(self):
        old = self.product()
        self.update(version="首代Max")
        with self.assertRaises(ConflictError):
            self.store.save_products({"changes": [
                {"brand": "B", "model": "new", "version": "test"},
                {"id": old["id"], "revision": old["revision"], "version": "stale"}]})
        self.assertEqual(len(self.store.list_products()["items"]), 1)
        with self.assertRaises(ValidationError):
            self.store.save_products({"changes": [{"id": old["id"], "revision": self.product()["revision"], "brand": ""}]})
        db_path = self.store.db_path
        self.store.close()
        self.store = SOPStore(db_path, self.path, lambda: self.current)
        self.assertEqual(self.product()["id"], old["id"])
        self.assertEqual(self.product()["version"], "首代Max")
        count = self.store.db.execute("SELECT count(*) FROM catalog_versions").fetchone()[0]
        self.store.read_sales()
        self.store.record_sales()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM catalog_versions").fetchone()[0], count)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM catalog_versions")

    def test_snapshots_freeze_catalog_at_cutoff_and_do_not_relabel_old_history(self):
        self.plan(sop=321)
        old = self.store.manual_snapshot("原名称")
        self.current = datetime(2026, 10, 24, 0, tzinfo=TZ)
        self.update(version="新名称")
        self.assertEqual(self.store.get_snapshot(old["snapshot"]["id"])["products"][0]["version"], "Max")
        daily = next(p for p in self.store.snapshots()["items"] if p["business_date"] == "2026-10-23" and p["kind"] == "daily")
        frozen = self.store.get_snapshot(daily["id"])
        self.assertEqual(frozen["products"][0]["version"], "Max")
        self.assertEqual(frozen["plans"][0]["sop"], 321)
        new = self.store.manual_snapshot("新名称")
        self.assertEqual(new["products"][0]["version"], "新名称")
        self.assertNotEqual(new["meta"]["catalog_version_id"], old["meta"]["catalog_version_id"])

    def test_upgrade_seeds_missing_products_from_old_sales_versions(self):
        self.plan()
        old_db = self.store.db_path
        self.store.close()
        with sqlite3.connect(old_db) as connection:
            connection.execute("DROP TRIGGER catalog_no_delete")
            connection.execute("DROP TRIGGER catalog_no_update")
            connection.execute("DROP TABLE catalog_versions")
            connection.execute("DROP TABLE product_catalog")
        connection.close()
        self.source["products"] = []
        self.source["sales"] = []
        self.write_source()
        self.store = SOPStore(old_db, self.path, lambda: self.current)
        product = self.product()
        self.assertEqual(product["version"], "Max")
        self.assertFalse(product["source_present"])
        self.assertEqual(self.store.state()["plans"][0]["sop"], 10)

    def test_manual_versions_strip_year_prefix_and_require_configuration(self):
        self.assertEqual(self.update(version="2026款 · 首代Max")["version"], "首代Max")
        with self.assertRaises(ValidationError):
            self.update(version="2026款")

    def test_explicit_binding_keeps_target_plans_and_remaps_sales_without_duplicates(self):
        target = self.store.save_products({"changes": [{"brand": "问界", "model": "M5预建", "version": "筹备Max"}]})["items"][0]
        self.plan(target["id"], "2027-01", 999)
        before = self.store.manual_snapshot("关联前")
        self.current += timedelta(seconds=1)
        bound = self.update(target["id"], bind_source_id="stable-2026")
        self.assertEqual(bound["model"], "M5预建")
        self.assertEqual(bound["version"], "筹备Max")
        self.assertEqual(bound["generation"], "问界 M5 2026款")
        self.assertEqual(self.store.source_aliases(), {"stable-2026": target["id"]})
        self.assertEqual([p["id"] for p in self.store.read_sales()["products"]], [target["id"]])
        self.assertEqual(self.store.read_sales()["sales"][0]["product_id"], target["id"])
        self.assertEqual(self.store.state()["plans"][0]["sop"], 999)
        after = self.store.manual_snapshot("关联后")
        self.assertEqual(after["sales"][0]["product_id"], target["id"])
        self.assertEqual(self.store.get_snapshot(before["snapshot"]["id"])["sales"][0]["product_id"], "stable-2026")
        self.assertEqual(len(self.store.get_snapshot(before["snapshot"]["id"])["products"]), 2)

    def test_binding_persists_and_source_updates_do_not_reset_target_label_or_revision(self):
        target = self.store.save_products({"changes": [{"brand": "问界", "model": "M5", "version": "预建Max"}]})["items"][0]
        self.update(target["id"], bind_source_id="stable-2026")
        self.source["products"][0]["version"] = "2027款 · Ultra"
        self.source["products"][0]["generation"] = "问界 M5 新来源代际"
        self.source["sales"][0]["value"] = 789
        self.write_source()
        self.store.record_sales()
        current = self.product(target["id"])
        self.assertEqual(current["version"], "预建Max")
        self.assertEqual(current["generation"], "问界 M5 新来源代际")
        self.assertEqual(self.store.read_sales()["sales"][0]["value"], 789)
        revision = current["revision"]
        self.store.record_sales()
        self.assertEqual(self.product(target["id"])["revision"], revision)
        db_path = self.store.db_path
        self.store.close()
        self.store = SOPStore(db_path, self.path, lambda: self.current)
        self.assertEqual(self.store.source_aliases()["stable-2026"], target["id"])
        self.assertEqual(len(self.store.state()["products"]), 1)

    def test_binding_rejects_source_with_plan_even_null_or_shipments_atomically(self):
        target = self.store.save_products({"changes": [{"brand": "B", "model": "M", "version": "V"}]})["items"][0]
        self.plan(sop=None)
        with self.assertRaises(ValidationError):
            self.update(target["id"], version="不应写入", bind_source_id="stable-2026")
        self.assertEqual(self.product(target["id"])["version"], "V")
        self.assertEqual(self.store.source_aliases(), {})
        self.source["products"].append({"id": "source-with-shipment", "brand": "B", "model": "N", "version": "V"})
        self.write_source()
        self.store.record_sales()
        self.store.save_shipments({"owner": "test", "changes": [{"product_id": "source-with-shipment", "month": "2026-09", "quantity": 0}]})
        with self.assertRaises(ValidationError):
            self.update(target["id"], bind_source_id="source-with-shipment")
        self.assertEqual(self.store.source_aliases(), {})

    def test_binding_rejects_repeated_chained_or_overlapping_sources(self):
        self.source["products"].append({"id": "second-source", "brand": "问界", "model": "M5", "version": "新Max"})
        self.source["sales"].append({"product_id": "second-source", "month": "2026-09", "metric": "locked_orders", "value": 1})
        self.write_source()
        self.store.record_sales()
        with self.assertRaises(ValidationError):
            self.update(bind_source_id="second-source")
        self.assertEqual(self.store.source_aliases(), {})
        target = self.store.save_products({"changes": [{"brand": "B", "model": "M", "version": "V"}]})["items"][0]
        self.update(target["id"], bind_source_id="second-source")
        with self.assertRaises(ValidationError):
            self.update(bind_source_id="second-source")
        with self.assertRaises(ValidationError):
            self.update(bind_source_id=target["id"])

    def test_renamed_source_can_bind_to_old_identity_and_refresh_is_stable(self):
        self.plan()
        self.source["products"] = [{"id": "renamed-source", "brand": "问界", "model": "M5来源新名", "version": "Max", "generation": "新来源"}]
        self.source["sales"] = [{"product_id": "renamed-source", "month": "2026-09", "metric": "locked_orders", "value": 5}]
        self.write_source()
        self.store.record_sales()
        result = self.update(bind_source_id="renamed-source")
        self.assertEqual(result["id"], "stable-2026")
        self.assertEqual(result["model"], "M5")
        self.assertEqual(result["generation"], "新来源")
        self.assertEqual(set(result["source_generations"]), {"问界 M5 2026款", "新来源"})
        self.assertEqual(self.store.state()["plans"][0]["product_id"], "stable-2026")
        self.assertEqual(self.store.read_sales()["sales"][0]["product_id"], "stable-2026")
        self.store.record_sales()
        self.assertEqual(self.product()["revision"], result["revision"])

    def test_manual_lifecycle_does_not_pollute_past_matching(self):
        self.store.save_shipments({"owner": "test", "changes": [{"product_id": "stable-2026", "month": "2026-09", "quantity": 100}]})
        before = self.store.matching("2026-09")
        created = self.store.save_products({"changes": [{"brand": "B", "model": "Future", "version": "V"}]})["items"][0]
        self.assertEqual(created["start_month"], "2026-10")
        self.assertEqual(self.product()["start_month"], "1900-01")
        after = self.store.matching("2026-09")
        self.assertEqual(before["actual"], 100)
        self.assertEqual(after["actual"], 100)
        self.assertEqual(after["product_count"], 1)
        with self.assertRaises(ValidationError):
            self.plan(created["id"], "2026-09")
        self.plan(created["id"], "2026-10", 0)
        self.plan(month="2026-01", sop=42)  # Missing source sales in Jan does not close it.

    def test_inactive_end_is_fixed_even_when_later_form_sends_null_and_restore_clears_it(self):
        stopped = self.update(active=False, end_month=None)
        self.assertEqual(stopped["end_month"], "2026-09")
        self.current = datetime(2026, 12, 1, 12, tzinfo=TZ)
        still = self.update(version="归档Max", active=False, end_month=None)
        self.assertEqual(still["end_month"], "2026-09")
        with self.assertRaises(ValidationError):
            self.plan(month="2026-10")
        resumed = self.update(active=True, end_month="2026-09")
        self.assertIsNone(resumed["end_month"])
        self.plan(month="2026-10")

    def test_lifecycle_changes_preserve_existing_values_and_allow_only_historical_correction(self):
        self.plan(month="2026-09", sop=0)
        self.plan(month="2027-01", sop=123)
        frozen = self.store.manual_snapshot("范围调整前")
        self.update(start_month="2026-10", end_month="2026-12")
        self.plan(month="2026-09", sop=7, revision=1)
        with self.assertRaises(ValidationError):
            self.plan(month="2027-01", sop=124, revision=1)
        self.assertEqual(next(p for p in self.store.state()["plans"] if p["month"] == "2027-01")["sop"], 123)
        self.assertEqual(self.store.matching("2026-09")["product_count"], 1)
        self.assertEqual(self.store.get_snapshot(frozen["snapshot"]["id"])["products"][0]["start_month"], "1900-01")
        self.update(start_month=None, end_month=None)
        self.assertIsNone(self.product()["start_month"])
        self.store.record_sales()
        self.assertIsNone(self.product()["start_month"])

    def test_binding_rejects_same_month_model_references_even_when_version_sales_are_empty(self):
        self.source["products"].append({"id": "new-source", "brand": "问界", "model": "M5", "version": "新Max", "generation": "问界 M5 2027款"})
        self.source["sales"] = []
        self.source["model_sales"] = [
            {"generation": "问界 M5 2026款", "month": "2026-09", "metric": "locked_orders", "value": 200},
            {"generation": "问界 M5 2027款", "month": "2026-09", "metric": "locked_orders", "value": 200}]
        self.write_source()
        self.store.record_sales()
        revision = self.product()["revision"]
        with self.assertRaisesRegex(ValidationError, "多个代际"):
            self.update(bind_source_id="new-source", version="不能被写入")
        self.assertEqual(self.store.source_aliases(), {})
        self.assertEqual(self.product()["revision"], revision)
        self.assertEqual(self.product()["version"], "Max")

    def test_alias_refresh_rejects_new_generation_overlap_without_mutating_catalog_or_history(self):
        self.source["products"].append({"id": "new-source", "brand": "问界", "model": "M5", "version": "新Max", "generation": "问界 M5 2027款"})
        self.source["sales"] = []
        self.source["model_sales"] = [
            {"generation": "问界 M5 2026款", "month": "2026-08", "metric": "locked_orders", "value": 200},
            {"generation": "问界 M5 2027款", "month": "2026-09", "metric": "locked_orders", "value": 200}]
        self.write_source()
        self.store.record_sales()
        self.update(bind_source_id="new-source")
        old_catalog = self.store.db.execute("SELECT count(*) FROM catalog_versions").fetchone()[0]
        old_sales = self.store.db.execute("SELECT count(*) FROM sales_versions").fetchone()[0]
        self.source["products"][1]["generation"] = "问界 M5 第三代"
        self.source["model_sales"].append({"generation": "问界 M5 第三代", "month": "2026-09", "metric": "locked_orders", "value": 0})
        with self.assertRaisesRegex(ValidationError, "多个代际"):
            self.store.validate_source_data(self.source)
        self.write_source()
        with self.assertRaisesRegex(ValidationError, "多个代际"):
            self.store.record_sales()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM catalog_versions").fetchone()[0], old_catalog)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sales_versions").fetchone()[0], old_sales)

    def test_alias_model_reference_allows_unknown_second_value_but_rejects_two_zeros(self):
        self.source["products"].append({"id": "new-source", "brand": "问界", "model": "M5", "version": "新Max", "generation": "问界 M5 2027款"})
        self.source["sales"] = []
        self.source["model_sales"] = [
            {"generation": "问界 M5 2026款", "month": "2026-09", "metric": "locked_orders", "value": 0},
            {"generation": "问界 M5 2027款", "month": "2026-09", "metric": "locked_orders", "value": None}]
        self.write_source()
        self.store.record_sales()
        self.update(bind_source_id="new-source")
        self.source["model_sales"][1]["value"] = 0
        with self.assertRaisesRegex(ValidationError, "多个代际"):
            self.store.validate_source_data(self.source)


if __name__ == "__main__":
    unittest.main()
