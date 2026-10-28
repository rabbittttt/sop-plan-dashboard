"""BETA presentation and group membership keep source identities and totals."""
import copy
import tempfile
import unittest
from pathlib import Path

from sop_source_rules import MergeRuleError, generation_model_key, load_merge_rules, project_generation_sources


class GenerationBetaTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sop-beta-rules-")
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "shared_config.py"
        self.groups = {"享界 X4 2026款汇总": ["享界 S9 2026款", "享界 S9T 2026款"]}

    def rules(self):
        self.config.write_text("MERGE_GROUPS = " + repr(self.groups), encoding="utf-8")
        return load_merge_rules(self.config)

    @staticmethod
    def source(products):
        return {"products": products,
                "sales": [{"product_id": p["id"], "month": "2026-10", "metric": "locked_orders", "value": i + 1}
                          for i, p in enumerate(products)],
                "model_sales": [{"generation": p["generation"], "brand": p["brand"], "model": p["model"],
                                 "month": "2026-10", "metric": "locked_orders", "value": (i + 1) * 10}
                                for i, p in enumerate(products) if p.get("generation")]}

    def test_detail_merges_beta_model_display_without_rekeying_editable_products(self):
        products = [{"id": "normal", "brand": "尊界", "model": "V800", "generation": "尊界 V800 2026款", "version": "Max"},
                    {"id": "beta", "brand": "尊界", "model": "V800 BETA", "generation": "尊界 V800 BETA 2026款", "version": "Max"},
                    {"id": "other", "brand": "尊界", "model": "BETAMAX", "generation": "尊界 BETAMAX 2026款", "version": "Max"}]
        data = self.source(products)
        before = copy.deepcopy(data)
        detail, _ = project_generation_sources(data, "detail", self.rules())
        self.assertEqual({p["id"]: p["model"] for p in detail["products"]},
                         {"normal": "V800", "beta": "V800", "other": "BETAMAX"})
        self.assertFalse(any(p.get("read_only") for p in detail["products"]))
        self.assertEqual(detail["sales"], data["sales"])
        self.assertEqual({p["id"]: p["generation"] for p in detail["products"]},
                         {p["id"]: p["generation"] for p in products})
        self.assertEqual(data, before)

    def test_summary_owns_beta_and_manual_members_once_without_using_raw_summary(self):
        products = [{"id": "a", "brand": "享界", "model": "S9", "generation": "享界 S9 2026款", "version": "Max"},
                    {"id": "beta", "brand": "享界", "model": "S9 BETA", "generation": "享界 S9 BETA 2026款", "version": "Max"},
                    {"id": "t", "brand": "享界", "model": "S9T", "generation": "享界 S9T 2026款", "version": "Max"},
                    {"id": "manual", "brand": "享界", "model": "S9", "version": "手工新版本"},
                    {"id": "raw", "brand": "享界", "model": "X4 BETA", "generation": "享界 X4 BETA 2026款汇总", "version": "Max"}]
        data = self.source(products)
        summary, warnings = project_generation_sources(data, "summary", self.rules())
        self.assertEqual({p["model"] for p in summary["products"]}, {"X4"})
        self.assertEqual({pid for p in summary["products"] for pid in p["member_product_ids"]}, {"a", "beta", "t", "manual"})
        self.assertTrue(all(p["read_only"] for p in summary["products"]))
        self.assertEqual(sum(r["value"] for r in summary["sales"]), 10)
        self.assertEqual(sum(r["value"] for r in summary["model_sales"]), 60)
        self.assertFalse(any("缺少单代际目录" in warning for warning in warnings))
        data["products"][-1]["generation"] = "享界 X4 2026款Beta汇总"
        data["model_sales"][-1]["generation"] = "享界 X4 2026款Beta汇总"
        alternate, _ = project_generation_sources(data, "summary", self.rules())
        self.assertEqual(alternate["products"], summary["products"])
        self.assertEqual(alternate["model_sales"], summary["model_sales"])

    def test_variants_and_other_years_follow_live_model_group(self):
        products = [{"id": "beta", "brand": "享界", "model": "S9（beta）", "generation": "享界\u3000S9（beta） 2026款", "version": "Max"},
                    {"id": "old", "brand": "享界", "model": "S9", "generation": "享界 S9 2025款", "version": "Max"}]
        data = self.source(products)
        self.groups = {"享界 X4 2026款汇总": ["享界 S9 2026款"]}
        first, _ = project_generation_sources(data, "summary", self.rules())
        group = next(p for p in first["products"] if p.get("read_only"))
        self.assertEqual(set(group["member_product_ids"]), {"beta", "old"})
        self.assertNotIn("old", {p["id"] for p in first["products"]})
        self.groups = {"享界 NEW 2026款汇总": ["享界 S9 2025款"]}
        second, _ = project_generation_sources(data, "summary", self.rules())
        group = next(p for p in second["products"] if p.get("read_only"))
        self.assertEqual(group["model"], "NEW")
        self.assertEqual(set(group["member_product_ids"]), {"beta", "old"})
        self.assertEqual(sum(r["value"] for r in second["model_sales"]), 30)

    def test_explicit_year_rules_win_and_ambiguous_other_year_is_rejected(self):
        self.groups = {"享界 Old 2025款汇总": ["享界 S9 2025款"],
                       "享界 New 2026款汇总": ["享界 S9 2026款"]}
        data = self.source([{"id": "p", "brand": "享界", "model": "S9", "generation": "享界 S9 2025款", "version": "Max"}])
        result, _ = project_generation_sources(data, "summary", self.rules())
        self.assertEqual({p["model"] for p in result["products"]}, {"Old"})
        data["products"][0]["generation"] = "享界 S9 2024款"
        data["model_sales"][0]["generation"] = "享界 S9 2024款"
        with self.assertRaises(MergeRuleError):
            project_generation_sources(data, "summary", self.rules())

    def test_supplied_41_generation_examples_never_leave_members_beside_summary(self):
        # User examples are regression fixtures, never a runtime model catalog.
        names = """问界 F2N 2026款汇总
问界 M5 2023款 纯电智驾版
问界 M5 2024款
问界 M5 2025款
问界 M6 2026款
问界 M7 2024款
问界 M7 2026款
问界 M8 2025款
问界 M9 2024款
问界 M9 2025款
问界 M9 2026款
问界 M9 Ultimate 2026款
智界 R7 2024款
智界 R7 2025款
智界 R7 2026款
智界 R7 2027款
智界 RX 2027款
智界 S7 2024款
智界 S7 2025款
智界 S7 2026款
智界 V9 2026款
享界 G9 2027款
享界 S9 2024款
享界 S9 2025款
享界 S9 2026款
享界 S9T 2026款
享界 V8 2027款
享界 X4 2026款汇总
尊界 S800 2025款
尊界 S800 典藏大观 2026款
尊界 V680 2026款
尊界 V800 2026款
尊界 V800 BETA 2026款
尊界 X6M 2026款汇总
尚界 H5 2026款
尚界 H5 2027款
尚界 SHB 2026款汇总
尚界 Z7 2025款Beta
尚界 Z7 2026款
尚界 Z7T 2025款Beta
尚界 Z7T 2026款""".splitlines()
        self.assertEqual(len(names), 41)
        self.groups = {"问界 F2N 2026款汇总": ["问界 M9 Ultimate 2026款", "问界 M9 2026款"],
                       "尊界 X6M 2026款汇总": ["尊界 V680 2026款", "尊界 V800 2026款"],
                       "尚界 SHB 2026款汇总": ["尚界 Z7 2026款", "尚界 Z7T 2026款"],
                       "享界 X4 2026款汇总": ["享界 S9 2026款", "享界 S9T 2026款"]}
        # Parse source model names with the real source parser, independently of projection matching.
        from sop_data import _product
        products = [{**_product(name, ""), "version": "Max"} for name in names]
        data = self.source(products)
        rules = self.rules()
        detail, _ = project_generation_sources(data, "detail", rules)
        result, _ = project_generation_sources(data, "summary", rules)
        models = {(p["brand"], p["model"]) for p in result["products"]}
        self.assertEqual(models, {("问界", m) for m in ["F2N", "M5", "M6", "M7", "M8"]}
                         | {("智界", m) for m in ["R7", "RX", "S7", "V9"]}
                         | {("享界", m) for m in ["G9", "X4", "V8"]}
                         | {("尊界", m) for m in ["S800", "S800 典藏大观", "X6M"]}
                         | {("尚界", m) for m in ["H5", "SHB"]})
        for field in ("sales", "model_sales"):
            self.assertEqual(sum(r["value"] for r in result[field]), sum(r["value"] for r in detail[field]))
        expected_members = {"X4": 4, "X6M": 3, "SHB": 4, "F2N": 4}
        self.assertEqual({p["model"]: len(p["member_product_ids"]) for p in result["products"] if p.get("read_only")}, expected_members)
        self.assertEqual(generation_model_key("尚界 Z7 2025款Beta"), ("尚界", "Z7"))

    def test_normalized_members_cannot_belong_to_two_groups(self):
        self.groups = {"享界 X4 2026款汇总": ["享界 S9 2026款"],
                       "享界 X5 2026款汇总": ["享界 S9 BETA 2026款"]}
        with self.assertRaises(MergeRuleError):
            self.rules()


if __name__ == "__main__":
    unittest.main()
