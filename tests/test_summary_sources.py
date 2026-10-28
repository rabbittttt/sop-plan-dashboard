"""Raw sources retain both levels; views apply live merge rules."""
import tempfile
import unittest
from pathlib import Path
import openpyxl
from sop_data import VERSION_METRICS, extract


class SummarySourceTest(unittest.TestCase):
    def test_raw_excel_keeps_members_and_summaries_for_switching_views(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for metric in VERSION_METRICS:
                book = openpyxl.Workbook()
                book.remove(book.active)
                for generation, locked in [("问界 M5 2025款", 100), ("问界 M5 2026款", 200), ("问界 M5 汇总", 300)]:
                    sheet = book.create_sheet(generation + "by月")
                    for row in [("指标", "统计类型", "分类", "26-10"), ("交车锁单", "数量", "数量", locked),
                                ("净大定", "数量", "数量", locked), ("版本", "数量", "Max", locked)]:
                        sheet.append(row)
                book.save(root / metric["source_file"])
                book.close()
            result = extract(root, as_of="2026-10-28", version_lock_basis="source_version_quantity")
        self.assertEqual(len(result["products"]), 3)
        self.assertEqual({p["generation"] for p in result["products"]},
                         {"问界 M5 2025款", "问界 M5 2026款", "问界 M5 汇总"})
        self.assertEqual([r["value"] for r in result["model_sales"]], [100, 200, 300])
        self.assertEqual(result["meta"]["source_schema_version"], 2)
        self.assertTrue(any(source["sheet"].endswith("汇总by月") for source in result["sources"]))


if __name__ == "__main__":
    unittest.main()
