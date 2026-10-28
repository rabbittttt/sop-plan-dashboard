"""Summary generation sheets must never duplicate the actual model/year rows."""
import copy
import tempfile
import unittest
from pathlib import Path

import openpyxl

from sop_data import VERSION_METRICS, extract
from sop_source_rules import exclude_summary_sources, is_summary_generation, is_summary_product


class SummarySourceTest(unittest.TestCase):
    def test_rule_uses_generation_suffix_not_model_or_version_labels(self):
        self.assertTrue(is_summary_generation("问界 M5 汇总  "))
        self.assertFalse(is_summary_generation("问界 汇总特别版 2026款"))
        self.assertFalse(is_summary_generation(None))
        self.assertFalse(is_summary_product({"model": "手工汇总", "version": "汇总"}))
        self.assertTrue(is_summary_product({"generation": "问界 M5 汇总"}))

    def test_filter_removes_summary_values_before_alias_remapping_without_mutation(self):
        source = {
            "products": [
                {"id": "old", "generation": "问界 M5 2025款"},
                {"id": "summary", "generation": "问界 M5 汇总"},
                {"id": "manual", "model": "M5", "version": "未来版本"},
                {"id": "mixed", "generation": "问界 M5 汇总", "source_generation": "问界 M5 汇总",
                 "source_generations": ["问界 M5 汇总", "问界 M5 2026款"]},
            ],
            "sales": [{"product_id": "old", "value": 100}, {"product_id": "summary", "value": 300}],
            "model_sales": [{"generation": "问界 M5 2025款", "value": 100},
                            {"generation": "问界 M5 汇总", "value": 300}],
            "version_reference": {"sales": [{"product_id": "summary", "value": 300}]},
            "sources": [{"sheet": "问界 M5 2025款by月"}, {"sheet": "问界 M5 汇总by月"}],
        }
        before = copy.deepcopy(source)
        result = exclude_summary_sources(source)
        self.assertEqual(source, before)
        self.assertEqual([p["id"] for p in result["products"]], ["old", "manual", "mixed"])
        self.assertEqual([r["value"] for r in result["sales"]], [100])
        self.assertEqual([r["value"] for r in result["model_sales"]], [100])
        self.assertEqual(result["version_reference"]["sales"], [])
        self.assertEqual(result["products"][-1]["generation"], "问界 M5 2026款")
        self.assertEqual(result["products"][-1]["source_generations"], ["问界 M5 2026款"])
        self.assertEqual(len(result["sources"]), 1)

    def test_real_excel_extract_keeps_two_years_and_excludes_their_summary(self):
        with tempfile.TemporaryDirectory(prefix="sop-summary-source-") as directory:
            root = Path(directory)
            for metric in VERSION_METRICS:
                book = openpyxl.Workbook()
                book.remove(book.active)
                for generation, model_lock, version_lock in (
                    ("问界 M5 2025款", 100, 80),
                    ("问界 M5 2026款", 200, 190),
                    ("问界 M5 汇总", 300, 270),
                    ("问界 M5 2026款汇总", 200, 190),
                ):
                    sheet = book.create_sheet(generation + "by月")
                    for row in [("指标", "统计类型", "分类", "26-10"),
                                ("交车锁单", "数量", "数量", model_lock),
                                ("净大定", "数量", "数量", model_lock),
                                ("版本", "数量", "Max", version_lock)]:
                        sheet.append(row)
                book.save(root / metric["source_file"])
                book.close()
            result = extract(root, as_of="2026-10-28", version_lock_basis="source_version_quantity")
        self.assertEqual(len(result["products"]), 2)
        self.assertEqual({p["model"] for p in result["products"]}, {"M5"})
        self.assertEqual({p["version"] for p in result["products"]}, {"Max"})
        self.assertEqual(len({p["id"] for p in result["products"]}), 2)
        self.assertEqual(sum(row["value"] for row in result["model_sales"]), 300)
        self.assertEqual(sum(row["value"] for row in result["sales"]), 270)
        self.assertEqual(result["meta"]["metric_coverage"]["locked_orders"]["model_monthly_total"], 300)
        self.assertTrue(all(not source["sheet"].endswith("汇总by月") for source in result["sources"]))


if __name__ == "__main__":
    unittest.main()
