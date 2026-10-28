"""Year boundaries also apply to source aliases and editable display labels."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sop_source_rules import MergeRuleError, project_generation_sources
from sop_store import SOPStore, TZ


class GenerationYearAliasTest(unittest.TestCase):
    def test_automatic_source_binding_requires_the_same_declared_year(self):
        with tempfile.TemporaryDirectory(prefix="sop-year-binding-") as directory:
            root = Path(directory)
            source = root / "sales.json"
            source.write_text(json.dumps({"meta": {}, "products": [], "sales": []}), encoding="utf-8")
            store = SOPStore(root / "test.sqlite3", source, lambda: datetime(2026, 10, 28, 12, tzinfo=TZ))
            try:
                items = store.save_products({"changes": [{"brand": "享界", "model": model, "version": "Max"}
                                                        for model in ("S9", "S9 2025款", "S9 2026款")]})["items"]
                ids = {p["model"]: p["id"] for p in items}
                source.write_text(json.dumps({"meta": {}, "products": [{"id": "new-source", "brand": "享界",
                                  "model": "S9", "generation": "享界 S9 2026款", "version": "Max"}], "sales": []}), encoding="utf-8")
                state = store.state()
                self.assertEqual(store.source_aliases(), {"new-source": ids["S9 2026款"]})
                self.assertEqual({p["id"] for p in state["products"]}, set(ids.values()))
                self.assertEqual({p["model"] for p in state["products"]}, {"S9", "S9 2025款", "S9 2026款"})
            finally:
                store.close()

    def test_partly_grouped_linked_generations_cannot_leak_into_summary(self):
        generations = ["享界 S9 2025款", "享界 S9 2026款"]
        product = {"id": "linked", "brand": "享界", "model": "S9", "version": "Max",
                   "generation": generations[-1], "source_generations": generations}
        data = {"products": [product], "sales": [
            {"product_id": "linked", "month": "2026-10", "metric": "locked_orders", "value": 10}],
            "model_sales": [{"generation": generation, "month": "2026-10", "metric": "locked_orders", "value": value}
                            for generation, value in zip(generations, [100, 200])]}
        rules = {"groups": {"享界 X4 2026款汇总": [generations[-1]]}}
        detail, _ = project_generation_sources(data, "detail", rules)
        self.assertEqual(detail["products"][0]["id"], "linked")
        self.assertEqual(detail["sales"][0]["value"], 10)
        with self.assertRaisesRegex(MergeRuleError, "已关联多个代际"):
            project_generation_sources(data, "summary", rules)
        # A shared rule may intentionally include every linked generation.
        rules["groups"]["享界 X4 2026款汇总"] = generations
        summary, _ = project_generation_sources(data, "summary", rules)
        self.assertEqual(summary["products"][0]["member_product_ids"], ["linked"])
        self.assertEqual(sum(row["value"] for row in summary["sales"]), 10)
        self.assertEqual(sum(row["value"] for row in summary["model_sales"]), 300)

    def test_version_edit_does_not_persist_beta_or_year_display_normalization(self):
        with tempfile.TemporaryDirectory(prefix="sop-year-catalog-") as directory:
            root = Path(directory)
            config = root / "shared_config.py"
            config.write_text("MERGE_GROUPS = {}\n", encoding="utf-8")
            source = root / "sales.json"
            product = {"id": "beta", "brand": "尊界", "model": "V800 BETA", "version": "Max",
                       "generation": "尊界 V800 BETA 2026款"}
            source.write_text(json.dumps({"meta": {}, "products": [product], "sales": []}), encoding="utf-8")
            with patch("sop_source_rules.DEFAULT_CONFIG_PATH", config):
                store = SOPStore(root / "test.sqlite3", source, lambda: datetime(2026, 10, 28, 12, tzinfo=TZ))
                try:
                    for model, version in [("V800 BETA", "Ultra"), ("V800 2026款", "Premium")]:
                        current = store.state()["products"][0]
                        store.save_products({"changes": [{"id": current["id"], "model": model,
                                             "version": version, "revision": current["revision"]}]})
                        stored = store.db.execute("SELECT source_json,overrides_json FROM product_catalog WHERE id='beta'").fetchone()
                        self.assertEqual(json.loads(stored["source_json"])["model"], "V800 BETA")
                        self.assertNotIn("model", json.loads(stored["overrides_json"]))
                        visible = store.state()["products"][0]
                        self.assertEqual((visible["model"], visible["version"]), ("V800 2026款", version))
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
