"""Read shared generation rules for each operation; never cache Python imports."""
import ast
import hashlib
import re
from collections import defaultdict
from pathlib import Path

CONFIG_RELATIVE_PATH = Path("..") / "订单分析" / "scripts" / "config" / "shared_config.py"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / CONFIG_RELATIVE_PATH
MODES = {"detail", "summary"}


class MergeRuleError(ValueError):
    pass


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
        groups, owners = {}, {}
        for summary, members in pairs:
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("汇总代际名称不能为空")
            summary = summary.strip()
            if summary in groups:
                raise ValueError(f"汇总代际重复：{summary}")
            if not isinstance(members, (list, tuple)) or not members:
                raise ValueError(f"汇总代际缺少单代际列表：{summary}")
            normalized = []
            for member in members:
                if not isinstance(member, str) or not member.strip():
                    raise ValueError(f"单代际名称不能为空：{summary}")
                member = member.strip()
                if member in owners:
                    raise ValueError(f"单代际重复归属：{member}")
                normalized.append(member)
                owners[member] = summary
            groups[summary] = normalized
        if set(groups) & set(owners):
            raise ValueError("汇总代际不能同时作为单代际成员，不能嵌套或循环汇总")
    except (OSError, UnicodeError, SyntaxError, ValueError, TypeError) as error:
        raise MergeRuleError(f"无法读取汇总代际规则 {label}：{error}。请恢复有效配置后重试。") from error
    return {"groups": groups, "signature": hashlib.sha256(source).hexdigest(), "config_path": label}


def _generations(product):
    values = [product.get("generation"), product.get("source_generation"),
              *(product.get("source_generations") or [])]
    return list(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip()))


def summary_names(rules):
    names = set(rules["groups"])
    members = {member for group in rules["groups"].values() for member in group}
    # Older source workbooks omit the conventional summary suffix in sheet names.
    names.update(name.removesuffix("汇总").strip() for name in rules["groups"]
                 if name.endswith("汇总") and name.removesuffix("汇总").strip() not in members)
    return names


def is_summary_generation(value, rules=None):
    return str(value or "").strip() in summary_names(rules or load_merge_rules())


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
    blocked = (set(rules["_excluded_generations"]) if "_excluded_generations" in rules else summary_names(rules)) if mode == "detail" else {member for members in groups.values() for member in members}
    removed = set(excluded_ids or ())
    removed.update(product["id"] for product in data.get("products", [])
                   if _generations(product) and all(g in blocked for g in _generations(product)))
    result = dict(data)
    if "products" in data:
        products = []
        for product in data["products"]:
            if product["id"] in removed:
                continue
            item = dict(product)
            actual = [g for g in _generations(product) if g not in blocked]
            if "source_generations" in item:
                item["source_generations"] = [g for g in item["source_generations"] if g not in blocked]
            for field in ("generation", "source_generation"):
                if item.get(field) in blocked and actual:
                    item[field] = actual[0]
            products.append(item)
        result["products"] = products
    for field in ("sales", "model_sales"):
        if field in data:
            result[field] = [row for row in data[field]
                             if row.get("product_id") not in removed and row.get("generation") not in blocked]
    if "sources" in data:
        result["sources"] = [source for source in data["sources"] if not isinstance(source, dict)
                             or str(source.get("sheet", "")).removesuffix("by月").strip() not in blocked]
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
    current_members = {member for members in rules["groups"].values() for member in members}
    excluded = summary_names(rules) | (set(known_summaries) - current_members)
    augmented = {**rules, "_excluded_generations": excluded}
    detail = select_generation_sources(data, "detail", augmented)
    groups = rules["groups"]
    warnings = []
    if mode == "detail":
        return detail, warnings
    products = detail.get("products", [])
    owners = {}
    for product in products:
        matches = [name for name, members in groups.items() if set(_generations(product)) & set(members)]
        if len(matches) > 1:
            raise MergeRuleError(f"版本跨多个汇总组关联，无法避免重复合计：{product.get('model', '')} {product.get('version', '')}")
        if matches:
            owners[product["id"]] = matches[0]
    result = dict(detail)
    result["products"] = [p for p in products if p["id"] not in owners]
    result["sales"] = [row for row in detail.get("sales", []) if row.get("product_id") not in owners]
    all_members = {member for members in groups.values() for member in members}
    result["model_sales"] = [row for row in detail.get("model_sales", []) if row.get("generation") not in all_members]
    by_id = {p["id"]: p for p in products}
    for summary, members in groups.items():
        members_set = set(members)
        selected = [p for p in products if owners.get(p["id"]) == summary]
        present = {generation for p in selected for generation in _generations(p) if generation in members_set}
        missing = [member for member in members if member not in present]
        if missing:
            warnings.append(f"{summary} 缺少单代际目录：{'、'.join(missing)}；未补0。")
        if not selected:
            continue
        versions = defaultdict(list)
        for product in selected:
            versions[product.get("version", "未命名版本")].append(product["id"])
        brand, _, model = summary.partition(" ")
        match = re.fullmatch(r"(.+?)\s+20\d{2}款.*", model.strip())
        model = match.group(1).strip() if match else model.removesuffix("汇总").strip()
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
        model_rows = defaultdict(dict)
        for row in detail.get("model_sales", []):
            if row.get("generation") in members_set:
                model_rows[(row["month"], row["metric"])][row["generation"]] = row.get("value")
        for (month, metric), values in model_rows.items():
            result["model_sales"].append({"brand": brand, "model": model, "generation": summary,
                "month": month, "metric": metric, "value": _sum_known(values.values()), "derived": True,
                "known_generations": sum(value is not None for value in values.values()),
                "total_generations": len(members)})
            if any(values.get(member) is None for member in members):
                warnings.append(f"{summary} {month} 锁单仅含已知单代际，仍有缺项。")
    # Reference drilldowns follow the same scope and cannot reintroduce members.
    for field in ("reference", "version_reference"):
        if isinstance(result.get(field), dict):
            result[field] = {**result[field], "sales": [row for row in result[field].get("sales", [])
                                                       if row.get("product_id") not in owners]}
    return result, warnings
