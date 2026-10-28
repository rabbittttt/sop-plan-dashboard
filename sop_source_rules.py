"""Presentation rules for source generations, without deleting stored history."""


def is_summary_generation(value):
    return str(value or "").strip().endswith("汇总")


def _generations(product):
    values = [product.get("generation"), product.get("source_generation"),
              *product.get("source_generations", [])]
    return list(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip()))


def is_summary_product(product):
    generations = _generations(product)
    # A manual product has no source generation. Mixed aliases may still refer
    # to a real generation and must retain their identity and stored plans.
    return bool(generations) and all(is_summary_generation(value) for value in generations)


def exclude_summary_sources(data, excluded_ids=None):
    """Return a filtered view; callers retain the original source and history."""
    removed = set(excluded_ids or ())
    removed.update(p["id"] for p in data.get("products", []) if is_summary_product(p))
    result = dict(data)
    if "products" in data:
        products = []
        for product in data["products"]:
            if product["id"] in removed:
                continue
            item = dict(product)
            actual = [value for value in _generations(product) if not is_summary_generation(value)]
            if "source_generations" in item:
                item["source_generations"] = [value for value in item["source_generations"]
                                              if not is_summary_generation(value)]
            for field in ("generation", "source_generation"):
                if is_summary_generation(item.get(field)) and actual:
                    item[field] = actual[0]
            products.append(item)
        result["products"] = products
    for field in ("sales", "model_sales"):
        if field in data:
            result[field] = [row for row in data[field]
                             if row.get("product_id") not in removed
                             and not is_summary_generation(row.get("generation"))]
    if "sources" in data:
        result["sources"] = [source for source in data["sources"]
                             if not isinstance(source, dict)
                             or not is_summary_generation(str(source.get("sheet", "")).removesuffix("by月"))]
    for field in ("reference", "version_reference"):
        if isinstance(data.get(field), dict):
            result[field] = exclude_summary_sources(data[field], removed)
    return result
