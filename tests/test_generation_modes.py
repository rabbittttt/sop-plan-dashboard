import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sop_source_rules import load_merge_rules, MergeRuleError, summary_names
from sop_store import SOPStore, TZ, ValidationError


class GenerationModeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sop-generation-view-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rules = self.root / "shared_config.py"
        self.groups = {"问界 M5 2026款汇总": ["问界 M5 2025款", "问界 M5 2026款"]}
        self.write_rules()
        self.patch = patch("sop_source_rules.DEFAULT_CONFIG_PATH", self.rules)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        products = [
            {"id": "a", "brand": "问界", "model": "M5", "version": "Max", "generation": "问界 M5 2025款"},
            {"id": "b", "brand": "问界", "model": "M5", "version": "Max", "generation": "问界 M5 2026款"},
            {"id": "sum", "brand": "问界", "model": "M5", "version": "Max", "generation": "问界 M5 2026款汇总"},
            {"id": "other", "brand": "智界", "model": "R7", "version": "Pro", "generation": "智界 R7 2026款"},
        ]
        self.data = {"meta": {}, "products": products,
                     "sales": [{"product_id": p["id"], "month": "2026-10", "metric": "locked_orders", "value": v}
                               for p, v in zip(products, [80, 190, 9999, 3])],
                     "model_sales": [{"generation": p["generation"], "month": "2026-10", "metric": "locked_orders", "value": v}
                                     for p, v in zip(products, [100, 200, 9999, 5])]}
        self.source = self.root / "sales.json"
        self.source.write_text(json.dumps(self.data), encoding="utf-8")
        self.store = SOPStore(self.root / "test.sqlite3", self.source,
                              lambda: datetime(2026, 10, 28, 12, tzinfo=TZ))
        self.addCleanup(self.store.close)
        self.store.save_plans({"owner": "test", "changes": [
            {"product_id": "a", "month": "2026-10", "forecast": None, "sop": 0, "revision": 0},
            {"product_id": "b", "month": "2026-10", "forecast": None, "sop": 20, "revision": 0},
            {"product_id": "other", "month": "2026-10", "forecast": None, "sop": 3, "revision": 0}]})

    def write_rules(self):
        self.rules.write_text("import os\nraise RuntimeError('must never execute')\nMERGE_GROUPS = " + repr(self.groups), encoding="utf-8")

    def test_modes_are_exclusive_and_summary_plans_are_derived_readonly(self):
        detail = self.store.state()
        self.assertEqual({p["id"] for p in detail["products"]}, {"a", "b", "other"})
        self.assertEqual(sum(row["value"] for row in detail["model_sales"]), 305)
        self.store.update_settings({"generation_mode": "summary"})
        summary = self.store.state()
        projected = next(p for p in summary["products"] if p.get("read_only"))
        self.assertEqual(projected["member_product_ids"], ["a", "b"])
        self.assertEqual({p["id"] for p in summary["products"]}, {projected["id"], "other"})
        self.assertEqual(sum(row["value"] for row in summary["model_sales"]), 305)
        self.assertEqual(sum(row["value"] for row in summary["sales"]), 273)
        self.assertEqual(next(row for row in summary["plans"] if row["product_id"] == projected["id"])["sop"], 20)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 3)
        with self.assertRaisesRegex(ValidationError, "只读"):
            self.store.save_plans({"owner": "test", "changes": [{"product_id": projected["id"],
                "month": "2026-10", "forecast": None, "sop": 999, "revision": 0}]})
        self.store.update_settings({"generation_mode": "detail"})
        self.assertEqual({p["id"] for p in self.store.state()["products"]}, {"a", "b", "other"})
        self.assertIn("sum", {p["id"] for p in self.store.read_sales(include_excluded=True)["products"]})

    def test_config_reloads_membership_and_retired_summary_stays_excluded(self):
        before = self.store.generation_view()["signature"]
        self.groups = {"问界 新组 2026款汇总": ["问界 M5 2026款"]}
        self.write_rules()
        self.store.update_settings({"generation_mode": "summary"})
        state = self.store.state()
        self.assertNotEqual(state["generation_view"]["signature"], before)
        self.assertNotIn("sum", {p["id"] for p in state["products"]})
        self.assertIn("a", {p["id"] for p in state["products"]})
        virtual = next(p for p in state["products"] if p.get("read_only"))
        self.assertEqual(virtual["member_product_ids"], ["b"])
        self.assertEqual(sum(row["value"] for row in state["model_sales"]), 305)
        self.rules.unlink()
        with self.assertRaises((MergeRuleError, ValidationError)):
            self.store.read_sales()

    def test_unknown_members_keep_missing_and_zero_distinct(self):
        self.store.save_plans({"owner": "test", "changes": [{"product_id": "b", "month": "2026-10",
                              "forecast": None, "sop": None, "revision": 1}]})
        self.groups = {"问界 新组 2026款汇总": ["问界 M5 2025款", "问界 未上市 2027款"]}
        self.write_rules()
        self.store.update_settings({"generation_mode": "summary"})
        state = self.store.state()
        virtual = next(p for p in state["products"] if p.get("read_only"))
        plan = next(p for p in state["plans"] if p["product_id"] == virtual["id"])
        self.assertEqual(plan["sop"], 0)
        self.assertTrue(plan["partial"])
        self.assertTrue(state["generation_view"]["warnings"])

    def test_alias_does_not_hide_explicit_member_and_invalid_groups_fail_closed(self):
        self.assertNotIn("问界 M5 2026款", summary_names(load_merge_rules()))
        invalid = ["MERGE_GROUPS = {'A':['X'], 'B':['X']}",
                   "MERGE_GROUPS = {'A':['B'], 'B':['A']}",
                   "MERGE_GROUPS = {'A':['X'], 'A':['Y']}",
                   "MERGE_GROUPS = dict(A=['X'])"]
        for source in invalid:
            with self.subTest(source=source):
                self.rules.write_text(source, encoding="utf-8")
                with self.assertRaises(MergeRuleError):
                    load_merge_rules()


if __name__ == "__main__":
    unittest.main()
