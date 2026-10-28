"""Read shared generation rules for each operation; never cache Python imports."""
import ast
import hashlib
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

CONFIG_RELATIVE_PATH = Path("..") / "订单分析" / "scripts" / "config" / "shared_config.py"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / CONFIG_RELATIVE_PATH
MODES = {"detail", "summary"}


class MergeRuleError(ValueError):
    pass


def normalize_generation(value):
    """Normalize labels for matching only; source identities remain unchanged."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"(20\d{2}款)\s*BETA(?=$|[\s_\-–—]|汇总$)", r"\1", value, flags=re.I)
    value = re.sub(r"[\s_\-–—]*[\(\[\{]\s*BETA\s*[\)\]\}][\s_\-–—]*", " ", value, flags=re.I)
    value = re.sub(r"(^|[\s_\-–—])BETA(?=$|[\s_\-–—])[\s_\-–—]*", " ", value, flags=re.I)
    return " ".join(value.split())


def generation_model_key(value):
    """Read a brand/model pair from a shared-rule generation, retaining its year elsewhere."""
    brand, _, model = normalize_generation(value).partition(" ")
    model = re.sub(r"\s+20\d{2}款.*$", "", model).removesuffix("汇总").strip()
    return brand, model


def _product_model_key(product):
    return tuple(normalize_generation(product.get(field)) for field in ("brand", "model"))


def generation_display_model(generation, model=None):
    """Keep year/qualifiers in the visible model; never change source identities."""
    source_model = normalize_generation(generation).partition(" ")[2].removesuffix("汇总").strip()
    model = normalize_generation(model) if model is not None else source_model
    suffix = re.search(r"(?:^|\s)(20\d{2}款.*)$", source_model)
    if not suffix:
        return model.removesuffix("汇总").strip()
    base = re.sub(r"(?:^|\s)20\d{2}款.*$", "", model).removesuffix("汇总").strip()
    return " ".join(part for part in (base, suffix.group(1)) if part)


def product_display_model(product):
    model = product.get("catalog_model", product.get("model", ""))
    labels = list(dict.fromkeys(generation_display_model(generation, model)
                               for generation in _generations(product)))
    return " / ".join(labels) if labels else normalize_generation(model)


def catalog_model_input(value, product):
    """Compare editable labels without their source-owned year annotation."""
    label = normalize_generation(value)
    suffixes = {match.group(1).removesuffix("汇总").strip()
                for generation in _generations(product)
                if (match := re.search(r"(?:^|\s)(20\d{2}款.*)$", normalize_generation(generation)))}
    if len(suffixes) == 1:
        suffix = next(iter(suffixes))
        if label.endswith(" " + suffix):
            return label[:-len(suffix)].strip()
    return label


def _declared_generations(product):
    """A manual label is a generation only when it explicitly names a year."""
    generations = _generations(product)
    if generations:
        return {normalize_generation(generation) for generation in generations}
    brand, model = _product_model_key(product)
    if re.search(r"(?:^|\s)20\d{2}款", model):
        return {normalize_generation(f"{brand} {model}")}
    return set()


def _group_candidates(product, member_owners):
    """Use only declared complete generations, with BETA as a same-year alias."""
    generations = _declared_generations(product)
    if not generations:
        return set()
    matches = {member_owners[generation] for generation in generations if generation in member_owners}
    unmapped = generations - member_owners.keys()
    if matches and unmapped:
        raise MergeRuleError("该版本已关联多个代际，只有部分代际属于当前汇总组；请先核对已关联来源并拆分跨代际记录："
                             + "、".join(sorted(generations)))
    return matches


def _display_labels(data):
    result = dict(data)
    if "products" in data:
        result["products"] = []
        for product in data["products"]:
            brand, model = _product_model_key(product)
            catalog_model = product.get("catalog_model", product.get("model", ""))
            model = product_display_model(product)
            item = {**product, "brand": brand, "model": model, "catalog_model": catalog_model}
            if (brand, model) != (product.get("brand"), product.get("model")):
                item["name"] = " ".join((brand, model, str(product.get("version") or ""))).strip()
            result["products"].append(item)
    if "model_sales" in data:
        result["model_sales"] = [{**row, "brand": normalize_generation(row.get("brand")),
                                  "model": generation_display_model(row.get("generation"), row.get("model", ""))}
                                 if "brand" in row or "model" in row else dict(row)
                                 for row in data["model_sales"]]
    return result


def load_merge_rules(path=None):
    """Read only the literal MERGE_GROUPS assignment; never execute the file."""
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    label = CONFIG_RELATIVE_PATH.as_posix() if path == DEFAULT_CONFIG_PATH else str(path)
    try:
        source = path.read_bytes()
        tree = ast.parse(source.decode("utf-8-sig"), filename=label)
        assignments = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                       and any(isinstance(target, ast.Name) and target.id == "MERGE_GROUPS"
                               for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
        if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Dict):
            raise ValueError("需要唯一的 MERGE_GROUPS 字典常量")
        literal = assignments[0].value
        pairs = [(ast.literal_eval(key), ast.literal_eval(value)) for key, value in zip(literal.keys, literal.values)]
        groups, owners, normalized_summaries = {}, {}, set()
        for summary, members in pairs:
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("汇总代际名称不能为空")
            summary = summary.strip()
            summary_key = normalize_generation(summary)
            if not summary_key or summary_key in normalized_summaries:
                raise ValueError(f"汇总代际重复：{summary}")
            normalized_summaries.add(summary_key)
            if not isinstance(members, (list, tuple)) or not members:
                raise ValueError(f"汇总代际缺少单代际列表：{summary}")
            normalized = []
            for member in members:
                if not isinstance(member, str) or not member.strip():
                    raise ValueError(f"单代际名称不能为空：{summary}")
                member = member.strip()
                member_key = normalize_generation(member)
                if not member_key or member_key in owners:
                    raise ValueError(f"单代际重复归属：{member}")
                normalized.append(member)
                owners[member_key] = summary
            groups[summary] = normalized
        if normalized_summaries & set(owners):
            raise ValueError("汇总代际不能同时作为单代际成员，不能嵌套或循环汇总")
    except (OSError, UnicodeError, SyntaxError, ValueError, TypeError) as error:
        raise MergeRuleError(f"无法读取汇总代际规则 {label}：{error}。请恢复有效配置后重试。") from error
    return {"groups": groups, "signature": hashlib.sha256(source).hexdigest(), "config_path": label}


def _generations(product):
    values = [product.get("generation"), product.get("source_generation"),
              *(product.get("source_generations") or [])]
    return list(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip()))


def summary_names(rules):
    names = {normalize_generation(name) for name in rules["groups"]}
    members = {normalize_generation(member) for group in rules["groups"].values() for member in group}
    # Older source workbooks omit the conventional summary suffix in sheet names.
    names.update(name.removesuffix("汇总").strip() for name in tuple(names)
                 if name.endswith("汇总") and name.removesuffix("汇总").strip() not in members)
    return names


def is_summary_generation(value, rules=None):
    return normalize_generation(value) in summary_names(rules or load_merge_rules())


def is_summary_product(product, rules=None):
    rules = rules or load_merge_rules()
    generations = _generations(product)
    return bool(generations) and all(is_summary_generation(value, rules) for value in generations)


def select_generation_sources(data, mode="detail", rules=None, excluded_ids=None):
    """Filter a copy by one exclusive view. Ungrouped and manual products remain."""
    if not isinstance(mode, str) or mode not in MODES:
        raise MergeRuleError("代际视图只能选择单代际或汇总代际。")
    rules = rules or load_merge_rules()
    groups = rules["groups"]
    blocked = ({normalize_generation(value) for value in rules["_excluded_generations"]} if "_excluded_generations" in rules else summary_names(rules)) if mode == "detail" else {normalize_generation(member) for members in groups.values() for member in members}
    removed = set(excluded_ids or ())
    removed.update(product["id"] for product in data.get("products", [])
                   if _generations(product) and all(normalize_generation(g) in blocked for g in _generations(product)))
    result = dict(data)
    if "products" in data:
        products = []
        for product in data["products"]:
            if product["id"] in removed:
                continue
            item = dict(product)
            actual = [g for g in _generations(product) if normalize_generation(g) not in blocked]
            if "source_generations" in item:
                item["source_generations"] = [g for g in item["source_generations"] if normalize_generation(g) not in blocked]
            for field in ("generation", "source_generation"):
                if normalize_generation(item.get(field)) in blocked and actual:
                    item[field] = actual[0]
            products.append(item)
        result["products"] = products
    for field in ("sales", "model_sales"):
        if field in data:
            result[field] = [row for row in data[field]
                             if row.get("product_id") not in removed and normalize_generation(row.get("generation")) not in blocked]
    if "sources" in data:
        result["sources"] = [source for source in data["sources"] if not isinstance(source, dict)
                             or normalize_generation(str(source.get("sheet", "")).removesuffix("by月")) not in blocked]
    for field in ("reference", "version_reference"):
        if isinstance(data.get(field), dict):
            result[field] = select_generation_sources(data[field], mode, rules, removed)
    return result


def exclude_summary_sources(data, excluded_ids=None):
    """Compatibility entry point for callers requesting the single-generation view."""
    return select_generation_sources(data, "detail", excluded_ids=excluded_ids)


def _sum_known(values):
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def project_generation_sources(data, mode, rules, known_summaries=()):
    """Build a read-only summary view from single-generation identities only."""
    if not isinstance(mode, str) or mode not in MODES:
        raise MergeRuleError("代际视图只能选择单代际或汇总代际。")
    # History only remembers known summary identities, never old memberships.
    # Current membership always comes from the newly read shared file.
    current_members = {normalize_generation(member) for members in rules["groups"].values() for member in members}
    excluded = summary_names(rules) | ({normalize_generation(name) for name in known_summaries} - current_members)
    augmented = {**rules, "_excluded_generations": excluded}
    detail = _display_labels(select_generation_sources(data, "detail", augmented))
    groups = rules["groups"]
    warnings = []
    if mode == "detail":
        return detail, warnings
    products = detail.get("products", [])
    owners = {}
    normalized_groups = {name: {normalize_generation(member) for member in members} for name, members in groups.items()}
    member_owners = {member: name for name, members in normalized_groups.items() for member in members}
    for product in products:
        matches = _group_candidates(product, member_owners)
        if len(matches) > 1:
            raise MergeRuleError(f"版本跨多个汇总组关联，无法避免重复合计：{product.get('model', '')} {product.get('version', '')}")
        if matches:
            owners[product["id"]] = next(iter(matches))
    model_owners = {}
    for index, row in enumerate(detail.get("model_sales", [])):
        matches = _group_candidates(row, member_owners)
        if len(matches) > 1:
            raise MergeRuleError(f"车型锁单跨多个汇总组关联，无法避免重复合计：{row.get('generation') or row.get('model', '')}")
        if matches:
            model_owners[index] = next(iter(matches))
    result = dict(detail)
    result["products"] = [p for p in products if p["id"] not in owners]
    result["sales"] = [row for row in detail.get("sales", []) if row.get("product_id") not in owners]
    result["model_sales"] = [row for index, row in enumerate(detail.get("model_sales", [])) if index not in model_owners]
    by_id = {p["id"]: p for p in products}
    for summary, members in groups.items():
        selected = [p for p in products if owners.get(p["id"]) == summary]
        present = {generation for product in selected for generation in _declared_generations(product)}
        missing = [member for member in members if normalize_generation(member) not in present]
        if missing:
            warnings.append(f"{summary} 缺少单代际目录：{'、'.join(missing)}；未补0。")
        versions = defaultdict(list)
        for product in selected:
            versions[product.get("version", "未命名版本")].append(product["id"])
        brand = normalize_generation(summary).partition(" ")[0]
        model = generation_display_model(summary)
        for version, ids in versions.items():
            pid = "group_" + hashlib.sha256((summary + "\x1f" + version).encode()).hexdigest()[:20]
            result["products"].append({"id": pid, "brand": brand, "model": model, "version": version,
                "name": f"{brand} {model} {version}", "generation": summary, "source_generation": summary,
                "source_generations": [summary], "source_ids": [], "source_backed": False,
                "source_present": any(by_id[item].get("source_present", True) for item in ids),
                "active": any(by_id[item].get("active", True) for item in ids), "revision": 0,
                "read_only": True, "member_product_ids": ids, "summary_generation": summary,
                "member_generations": list(members), "missing_member_generations": missing,
                "start_month": None, "end_month": None})
            grouped = defaultdict(list)
            for row in detail.get("sales", []):
                if row.get("product_id") in ids:
                    grouped[(row["month"], row["metric"])].append(row.get("value"))
            for (month, metric), values in grouped.items():
                result["sales"].append({"product_id": pid, "month": month, "metric": metric,
                                        "value": _sum_known(values), "derived": True})
        model_rows = defaultdict(list)
        for index, row in enumerate(detail.get("model_sales", [])):
            if model_owners.get(index) == summary:
                # BETA and standard source rows are distinct contributions.
                # Never key quantities by a normalized label and overwrite one.
                model_rows[(row["month"], row["metric"])].append(row)
        for (month, metric), rows in model_rows.items():
            known = {generation for row in rows if row.get("value") is not None
                     for generation in _declared_generations(row)}
            result["model_sales"].append({"brand": brand, "model": model, "generation": summary,
                "month": month, "metric": metric, "value": _sum_known(row.get("value") for row in rows), "derived": True,
                "known_generations": sum(normalize_generation(member) in known for member in members),
                "total_generations": len(members)})
            if any(normalize_generation(member) not in known for member in members) or any(row.get("value") is None for row in rows):
                warnings.append(f"{summary} {month} 锁单仅含已知单代际，仍有缺项。")
    # Reference drilldowns follow the same scope and cannot reintroduce members.
    for field in ("reference", "version_reference"):
        if isinstance(result.get(field), dict):
            result[field] = {**result[field], "sales": [row for row in result[field].get("sales", [])
                                                       if row.get("product_id") not in owners]}
    return result, warnings
