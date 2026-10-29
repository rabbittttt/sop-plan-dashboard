"""Model-year totals share the plan ledger, but never add to version totals."""
from collections import defaultdict

from sop_source_rules import normalize_generation, product_display_model

MODEL_PLAN_LABEL = "车型总量（不分版本）"


def is_model_plan(product):
    return product.get("plan_level") == "model" or product.get("version") == MODEL_PLAN_LABEL


def effective_products(products, month, plans, field="sop"):
    groups = defaultdict(list)
    for product in products:
        groups[(normalize_generation(product.get("brand")), product_display_model(product))].append(product)
    selected = []
    for members in groups.values():
        totals = [p for p in members if is_model_plan(p)
                  and plans.get((p["id"], month), {}).get(field) is not None]
        selected.extend(totals if field == "sop" and totals else [p for p in members if not is_model_plan(p)])
    return selected


def plan_total(products, month, plans, field="sop"):
    values = [plans.get((p["id"], month), {}).get(field)
              for p in effective_products(products, month, plans, field)]
    known = [value for value in values if value is not None]
    return sum(known) if known else None
