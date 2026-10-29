"""Atomic, revision-checked saves for the editable planning grid."""
import hashlib
import json
import re
import uuid

from sop_plan_scope import MODEL_PLAN_LABEL, is_model_plan

from sop_source_rules import generation_display_model, normalize_generation, project_generation_sources
from sop_store import (ConflictError, TZ, ValidationError, catalog_name_key, display_version,
                       horizon, product_display_model, quantity, stamp, text_field, validate_month)


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("修订号必须为非负整数，请刷新表格后重试。")
    return value


def save_grid_batch(store, body):
    allowed = {"request_id", "owner", "reason", "notes", "generation_mode", "generation_rule_signature",
               "changes", "catalog_changes", "entries"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValidationError("表格批量维护请求格式无效。")
    try:
        if not isinstance(body.get("request_id"), str):
            raise ValueError()
        request_id = str(uuid.UUID(body["request_id"]))
        digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("保存请求编号或内容无效，请重新开始批量维护。")
    owner = text_field(body.get("owner"), "维护人", 100)
    if not owner:
        raise ValidationError("请填写维护人。")
    reason = text_field(body.get("reason") or "网页批量维护", "变更原因")
    notes = text_field(body.get("notes"), "备注", 5000)
    changes, edits, entries = (body.get(field, []) for field in ("changes", "catalog_changes", "entries"))
    if any(not isinstance(value, list) or len(value) > limit
           for value, limit in ((changes, 20000), (edits, 2000), (entries, 2000))):
        raise ValidationError("批量维护内容过多或格式无效。")

    with store.lock:
        receipt = store.db.execute("SELECT digest,result FROM grid_batch_requests WHERE request_id=?", (request_id,)).fetchone()
        if receipt:
            if receipt["digest"] != digest:
                raise ValidationError("同一保存编号不能用于不同修改，请重新保存。")
            return json.loads(receipt["result"])
        store.ensure_daily_snapshots()
        sales = store.read_sales()
        view = sales.get("generation_view", {})
        if (body.get("generation_mode") != view.get("mode")
                or body.get("generation_rule_signature") != view.get("rule_signature")):
            raise ValidationError("代际范围或共享规则已变化，请重新加载表格后核对修改。")
        visible = {p["id"]: p for p in sales["products"]}
        raw = {p["id"]: p for p in store._catalog_items()}
        rules = store._generation_rules()
        if rules["signature"] != view.get("rule_signature"):
            raise ValidationError("共享规则正在变化，请重新加载表格后重试。")
        current = store.clock().astimezone(TZ)
        current_month, recorded_at = current.strftime("%Y-%m"), stamp(current)
        plan_end = horizon(current_month, store.settings()["bridge_m6"])

        def editable(pid):
            if not isinstance(pid, str) or pid not in visible:
                raise ValidationError("版本不在当前表格范围内，请刷新后重试。")
            if visible[pid].get("read_only"):
                raise ValidationError("汇总代际只读，请切换至单代际维护。")
            if pid not in raw:
                raise ValidationError("版本不在当前目录内，请刷新后重试。")
            return raw[pid]

        store.db.execute("BEGIN IMMEDIATE")
        try:
            # Another service connection may have completed the same request
            # while this connection was waiting for its SQLite write lock.
            receipt = store.db.execute("SELECT digest,result FROM grid_batch_requests WHERE request_id=?", (request_id,)).fetchone()
            if receipt:
                if receipt["digest"] != digest:
                    raise ValidationError("同一保存编号不能用于不同修改，请重新保存。")
                store.db.execute("COMMIT")
                return json.loads(receipt["result"])
            latest_view = store.generation_view()
            if latest_view["signature"] != view["signature"]:
                raise ValidationError("代际范围或共享规则已变化，请重新加载表格后核对修改。")
            # Recheck catalog revisions under the database write lock too.
            raw = {p["id"]: p for p in store._catalog_items()}
            conflicts, trusted_edits, touched, deleted = [], [], set(), set()
            for change in edits:
                fields = {"id", "revision", "brand", "model", "version", "active", "plan_revisions"}
                if not isinstance(change, dict) or set(change) - fields:
                    raise ValidationError("版本行修改格式无效。")
                pid = change.get("id")
                prior = editable(pid)
                if pid in touched:
                    raise ValidationError("同一版本行不能重复提交。")
                touched.add(pid)
                if _revision(change.get("revision")) != prior["revision"]:
                    conflicts.append({"kind": "catalog", "product_id": pid, "current": prior})
                values = {field: change[field] for field in ("brand", "model", "version", "active") if field in change}
                if not values:
                    raise ValidationError("版本行未提供修改内容。")
                trusted = {"id": pid, "baseline": {field: prior.get(field) for field in values}, **values}
                if "active" in values:
                    if values["active"] is not False:
                        raise ValidationError("删除行按停用处理；启用请使用车型与版本维护。")
                    revisions = change.get("plan_revisions")
                    if not isinstance(revisions, dict) or len(revisions) > 5000:
                        raise ValidationError("删除行缺少完整计划修订基准，请刷新后重试。")
                    expected = {validate_month(month): _revision(revision) for month, revision in revisions.items()}
                    actual = {row["month"]: row["revision"] for row in store.db.execute(
                        "SELECT month,revision FROM plans WHERE product_id=?", (pid,))}
                    if expected != actual:
                        conflicts.append({"kind": "catalog", "product_id": pid, "field": "active",
                                          "reason": "plans_changed", "message": "该版本计划已更新，请核对是否仍要删除行。"})
                    trusted["baseline_plan_revisions"] = expected
                    deleted.add(pid)
                trusted_edits.append(trusted)

            ordinary, seen = [], set()
            for change in changes:
                if not isinstance(change, dict) or set(change) != {"product_id", "month", "sop", "revision"}:
                    raise ValidationError("计划单元格修改格式无效。")
                pid = change.get("product_id")
                editable(pid)
                month = validate_month(change.get("month"))
                if month > plan_end:
                    raise ValidationError(f"{month}超出当前开放计划范围。")
                if pid in deleted:
                    raise ValidationError("删除版本行时请先撤销该行的数值修改。")
                key = pid, month
                if key in seen:
                    raise ValidationError("同一个计划单元格不能重复提交。")
                seen.add(key)
                sop, revision = quantity(change["sop"]), _revision(change["revision"])
                previous = store.db.execute("SELECT * FROM plans WHERE product_id=? AND month=?", key).fetchone()
                previous = dict(previous) if previous else {}
                if revision != previous.get("revision", 0):
                    conflicts.append({"product_id": pid, "month": month, "current": previous or None})
                if sop != previous.get("sop"):
                    ordinary.append({**change, "forecast": previous.get("forecast"), "notes": previous.get("notes", notes)})
            if conflicts:
                raise ConflictError(conflicts)

            updated, deactivated = store._merge_workbook_catalog(trusted_edits, None, recorded_at, current_month)
            catalog = {p["id"]: p for p in store._catalog_items()}
            names = {catalog_name_key({**p, "model": product_display_model(p)}) for p in catalog.values()}
            entry_ids, created = {}, []
            summary_models = {(normalize_generation(name).partition(" ")[0], generation_display_model(name))
                              for name in rules["groups"]}
            for entry in entries:
                if (not isinstance(entry, dict) or set(entry) - {"client_id", "brand", "model", "version", "plans", "plan_level"}
                        or not isinstance(entry.get("plans", []), list)):
                    raise ValidationError("新增版本行格式无效。")
                client_id = text_field(entry.get("client_id"), "新增行编号", 100)
                if not client_id or client_id in entry_ids:
                    raise ValidationError("新增行编号为空或重复。")
                labels = {}
                if entry.get("plan_level") not in (None, "model", "version"):
                    raise ValidationError("请选择车型或版本维护粒度。")
                for field, label in (("brand", "品牌"), ("model", "车型"), ("version", "版本")):
                    value = text_field(entry.get(field), label, 200)
                    value = display_version(value, "") if field == "version" else value
                    if field == "version" and (entry.get("plan_level") == "model" or not value):
                        value = MODEL_PLAN_LABEL
                    if not value:
                        raise ValidationError(f"新增行请填写{label}。")
                    labels[field] = value
                if is_model_plan(labels):
                    if not re.search(r"20\d{2}款", labels["model"]):
                        raise ValidationError("车型计划请填写带年款的车型，例如 M5 2026款。")
                    labels["plan_level"] = "model"
                name = catalog_name_key({**labels, "model": product_display_model(labels)})
                if name in names:
                    raise ValidationError("相同品牌、车型年款和版本已存在，请在已有行维护。")
                names.add(name)
                if (normalize_generation(labels["brand"]), normalize_generation(labels["model"])) in summary_models:
                    raise ValidationError("汇总代际由成员计算，请新增对应的单代际版本。")
                provisional, _ = project_generation_sources({"products": [{**labels, "id": "new-grid-row"}]}, view["mode"], rules)
                if not any(p["id"] == "new-grid-row" and not p.get("read_only") for p in provisional["products"]):
                    raise ValidationError("该新增版本属于汇总代际，请切换至单代际后新增。")
                values, months = [], set()
                for cell in entry.get("plans", []):
                    if not isinstance(cell, dict) or set(cell) != {"month", "sop"}:
                        raise ValidationError("新增行计划格式无效。")
                    month, sop = validate_month(cell["month"]), quantity(cell["sop"])
                    if month > plan_end or month in months:
                        raise ValidationError("新增行月份重复或超出当前开放计划范围。")
                    months.add(month)
                    if sop is not None:
                        values.append((month, sop))
                if len(ordinary) + len(values) > 20000:
                    raise ValidationError("一次最多保存20000个计划单元格。")
                pid = "manual_" + uuid.uuid4().hex
                start = min([current_month, *(month for month, _ in values)])
                store.db.execute("""INSERT INTO product_catalog
                    (id,source_json,overrides_json,active,source_present,revision,created_at,updated_at,start_month)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (pid, "{}", json.dumps({**labels, "lifecycle_set": True}, ensure_ascii=False),
                    1, 0, 1, recorded_at, recorded_at, start))
                product = store._catalog_product(store.db.execute("SELECT * FROM product_catalog WHERE id=?", (pid,)).fetchone())
                catalog[pid] = product
                created.append(product)
                entry_ids[client_id] = pid
                ordinary.extend({"product_id": pid, "month": month, "sop": sop, "forecast": None, "revision": 0, "notes": notes}
                                for month, sop in values)

            prepared, _, _, _ = store._changes({"changes": ordinary, "owner": owner, "reason": reason, "notes": notes},
                                               "plan", products=catalog, allow_empty=True)
            saved = store._apply_changes(prepared, "plan", owner, reason, notes)
            if created or updated:
                store._record_catalog(owner, "网页批量维护车型与版本")
            result = {"ok": True, "items": saved, "entry_ids": entry_ids, "products": created,
                      "saved_count": len(saved), "created_count": len(created),
                      "catalog_updated_count": len(updated), "deactivated_count": len(deactivated)}
            store.db.execute("INSERT INTO grid_batch_requests(request_id,digest,result,created_at) VALUES (?,?,?,?)",
                             (request_id, digest, json.dumps(result, ensure_ascii=False), recorded_at))
            store.db.execute("COMMIT")
            return result
        except Exception:
            store.db.execute("ROLLBACK")
            raise
