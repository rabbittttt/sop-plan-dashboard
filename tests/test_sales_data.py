"""Regression checks: source metric ambiguity must not become version actuals."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sop_data


def fixture():
    products = [{"id": "stable-max", "brand": "问界", "model": "M5", "version": "2025款 · 后驱 Max",
                 "generation": "问界 M5 2025款", "configuration": "后驱 Max"},
                {"id": "stable-ultra", "brand": "问界", "model": "M5", "version": "2025款 · 四驱 Ultra",
                 "generation": "问界 M5 2025款", "configuration": "四驱 Ultra"}]
    rows = [{"product_id": "stable-max", "month": "2026-03", "metric": "delivery_reference", "value": 0,
             "source_id": "locks", "source_cell": "D19", "complete": False},
            {"product_id": "stable-ultra", "month": "2026-03", "metric": "delivery_reference", "value": 17,
             "source_id": "locks", "source_cell": "D20", "complete": False},
            {"product_id": "stable-max", "month": "2026-03", "metric": "net_orders", "value": 49,
             "source_id": "net", "source_cell": "D19", "complete": False}]
    return {"products": products, "sales": rows, "meta": {"metrics": [{"id": "delivery_reference"}, {"id": "net_orders"}],
            "quality": [{"code": "unknown_month_cutoff", "message": "截止日不明"}], "reconciliation": [],
            "data_through": "2026-03", "notes": []}, "sources": [{"id": "locks", "metric": "delivery_reference"}],
            "reference": {"sales": [{"metric": "locked_orders", "value": 9999}]}}, [
            {"brand": "问界", "model": "M5", "generation": "问界 M5 2025款", "model_year": "2025款",
             "month": "2026-03", "metric": "locked_orders", "value": 23, "source_id": "locks", "source_cell": "D2"}]


class SalesMetricSafetyTests(unittest.TestCase):
    def test_model_lock_row_ignores_wrong_version_values_and_removes_confirmation_warning(self):
        source, models = fixture()
        source["sales"][0]["value"] = 88888
        result = sop_data._build_lock_data(source, models, "model_lock_row")
        self.assertEqual(result["meta"]["default_metric"], "locked_orders")
        self.assertEqual(result["meta"]["version_lock_basis"], "model_lock_row")
        self.assertEqual(result["model_sales"][0]["value"], 23)
        self.assertEqual(result["meta"]["metric_coverage"]["locked_orders"]["model_monthly_total"], 23)
        self.assertTrue(all(r["value"] is None for r in result["sales"]))
        self.assertTrue(all(r["value_status"] == "not_available_at_version" for r in result["sales"]))
        self.assertTrue(all(r["coverage_status"] == "not_available_at_version" for r in result["sales"]))
        self.assertTrue(all("source_version_value" not in r for r in result["sales"]))
        self.assertEqual(result["products"], source["products"])
        codes = {q["code"] for q in result["meta"]["quality"]}
        self.assertIn("lock_basis_model_row", codes)
        self.assertNotIn("version_lock_control_difference", codes)
        self.assertNotIn("version_locks_unconfirmed", codes)
        self.assertEqual(result["sources"][0]["range"], "D2:D2")

    def test_model_extractor_reads_lock_row_and_preserves_real_zero(self):
        class Sheet:
            title = "问界 M5 2025款by月"

            def iter_rows(self, **kwargs):
                return iter([
                    ("指标", "统计类型", "分类", "26-03", "26-04", "总计"),
                    ("交车锁单", "数量", "数量", 23, 0, 23),
                    ("已交付", "数量", "数量", 17, 999, 1016),
                    ("版本", "数量", "后驱 Max", 88888, 88888, 88888),
                ])

        class Book(list):
            def close(self):
                pass

        with patch.object(sop_data.openpyxl, "load_workbook", return_value=Book([Sheet()])):
            rows = sop_data._model_monthly_locks(Path("unused"), as_of="2026-10-22")
        self.assertEqual([row["value"] for row in rows], [23, 0])
        self.assertEqual([row["source_cell"] for row in rows], ["D2", "E2"])
        self.assertTrue(all(row["metric"] == "locked_orders" and not row["is_allocated"] for row in rows))

    def test_version_reconciliation_uses_lock_total_not_delivery(self):
        class Sheet:
            title = "问界 M5 2025款by月"

            def iter_rows(self, **kwargs):
                return iter([
                    ("指标", "统计类型", "分类", "26-03"),
                    ("交车锁单", "数量", "数量", 23),
                    ("已交付", "数量", "数量", 17),
                    ("净大定", "数量", "数量", 49),
                    ("版本", "数量", "后驱 Max", 0),
                    (None, None, "四驱 Ultra", 17),
                ])

        class Book:
            worksheets = [Sheet()]

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for metric in sop_data.VERSION_METRICS:
                (root / metric["source_file"]).touch()
            with patch.object(sop_data.openpyxl, "load_workbook", return_value=Book()), \
                 patch.object(sop_data, "_option_consistency", return_value=[]):
                result = sop_data._extract_version_reference(root, as_of="2026-10-26")
        check = next(row for row in result["meta"]["reconciliation"]
                     if row["metric"] == "delivery_reference")
        self.assertEqual((check["version_sum"], check["source_total"], check["difference"]), (17, 23, -6))
        self.assertEqual((check["control_label"], check["source_cell"]), ("交车锁单", "D2"))
        self.assertEqual(sorted(row["value"] for row in result["sales"] if row["metric"] == "delivery_reference"), [0, 17])

    def test_model_lock_confirmation_persists_across_ordinary_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "source_settings.json").write_text(
                json.dumps({"version_lock_basis": "model_lock_row"}), encoding="utf-8")
            source, models = fixture()
            with patch.object(sop_data, "ROOT", root), \
                 patch.object(sop_data, "_extract_version_reference", return_value=source), \
                 patch.object(sop_data, "_model_monthly_locks", return_value=models):
                result = sop_data.extract()
            self.assertEqual(result["meta"]["version_lock_basis"], "model_lock_row")
            self.assertTrue(all(row["value"] is None for row in result["sales"]))
            self.assertEqual(result["model_sales"][0]["value"], 23)

    def test_unconfirmed_configuration_quantities_stay_unknown(self):
        source, models = fixture()
        result = sop_data._build_lock_data(source, models, "unconfirmed")
        self.assertEqual(result["meta"]["default_metric"], "locked_orders")
        self.assertTrue(all(r["value"] is None for r in result["sales"]))
        self.assertIsNone(result["meta"]["metric_coverage"]["locked_orders"]["monthly_total"])
        self.assertEqual(result["model_sales"][0]["value"], 23)
        self.assertEqual(result["products"], source["products"])
        self.assertEqual(result["meta"]["reconciliation"][0]["difference"], -6)
        self.assertFalse(any(r["is_allocated"] for r in result["sales"]))

    def test_confirmed_basis_preserves_raw_zero_and_does_not_use_net(self):
        source, models = fixture()
        before = copy.deepcopy(source)
        result = sop_data._build_lock_data(source, models, "source_version_quantity")
        self.assertEqual([r["value"] for r in result["sales"]], [0, 17])
        self.assertEqual([r["source_cell"] for r in result["sales"]], ["D19", "D20"])
        self.assertEqual(result["meta"]["metric_coverage"]["locked_orders"]["monthly_total"], 17)
        self.assertEqual(result["model_sales"][0]["value"], 23)
        self.assertEqual(result["model_sales"][0]["source_cell"], "D2")
        discrepancy = next(q for q in result["meta"]["quality"] if q["code"] == "version_lock_control_difference")
        self.assertEqual(discrepancy["level"], "info")
        self.assertEqual(discrepancy["count"], 1)
        self.assertIn("不强制相等", discrepancy["message"])
        self.assertIn("版本/数量", result["sources"][0]["description"])
        self.assertIn("已作为版本锁单", result["version_reference"]["description"])
        self.assertEqual(source, before)
        self.assertEqual([p["id"] for p in result["products"]], ["stable-max", "stable-ultra"])

    def test_missing_version_row_is_not_derived_from_other_metric(self):
        source, models = fixture()
        source["sales"] = [r for r in source["sales"] if r["metric"] == "net_orders"]
        result = sop_data._build_lock_data(source, models, "source_version_quantity")
        self.assertEqual(result["sales"], [])
        self.assertEqual(len(result["products"]), 2)
        self.assertIsNone(result["meta"]["metric_coverage"]["locked_orders"]["monthly_total"])

    def test_persistent_confirmation_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source_settings.json"
            self.assertEqual(sop_data._resolve_lock_basis(settings_path=path), "unconfirmed")
            path.write_text(json.dumps({"version_lock_basis": "source_version_quantity"}), encoding="utf-8")
            self.assertEqual(sop_data._resolve_lock_basis(settings_path=path), "source_version_quantity")
            self.assertEqual(sop_data._resolve_lock_basis("unconfirmed", path), "unconfirmed")
            path.write_text('{"version_lock_basis": "typo"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                sop_data._resolve_lock_basis(settings_path=path)

    def test_default_refresh_uses_input_directory(self):
        source, models = fixture()
        with patch.object(sop_data, "_extract_version_reference", return_value=source) as extract_versions, \
             patch.object(sop_data, "_model_monthly_locks", return_value=models), \
             patch.object(sop_data, "_resolve_lock_basis", return_value="unconfirmed"):
            sop_data.extract()
        self.assertEqual(extract_versions.call_args.args[0], sop_data.ROOT / "input_file")


if __name__ == "__main__":
    unittest.main()
