"""Persistent local SOP data, immutable audit events, and cutoff snapshots."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sop_brand import brand_order_key
from sop_source_rules import (MergeRuleError, load_merge_rules, summary_names, project_generation_sources,
                              _generations as source_generations)

TZ = timezone(timedelta(hours=8))
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
WEIGHTS = {**{i: 0.02 for i in range(9, 14)}, **{i: 0.075 for i in range(5, 9)},
           **{i: 0.10 for i in range(1, 5)}, 0: 0.20}


class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    def __init__(self, conflicts):
        super().__init__("数据已被更新，请先刷新后再保存。")
        self.conflicts = conflicts


def now_local():
    return datetime.now(TZ)


def stamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ).isoformat(timespec="microseconds")


def month_add(month, offset):
    validate_month(month)
    year, m = map(int, month.split("-"))
    ordinal = year * 12 + m - 1 + offset
    return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"


def validate_month(value):
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise ValidationError("月份格式必须为 YYYY-MM。")
    if not 1900 <= int(value[:4]) <= 2199:
        raise ValidationError("月份超出支持范围。")
    return value


def horizon(current_month, bridge_m6=True):
    validate_month(current_month)
    year, month = map(int, current_month.split("-"))
    annual = f"{year + (1 if month >= 9 else 0):04d}-12"
    return max(annual, month_add(current_month, 6)) if bridge_m6 else annual


def month_range(start, end):
    result = []
    cursor = start
    while cursor <= end:
        result.append(cursor)
        cursor = month_add(cursor, 1)
    return result


def sunday_before(month):
    first = date.fromisoformat(validate_month(month) + "-01")
    return first - timedelta(days=(first.weekday() + 1) % 7 or 7)


def week_zero_for_month(month):
    """Mon-Sun week: use prior week if the first week has >4 in-month days."""
    first = date.fromisoformat(validate_month(month) + "-01")
    if first.weekday() <= 2:  # Monday, Tuesday, Wednesday: seven, six, five days.
        return sunday_before(month)
    return first + timedelta(days=6 - first.weekday())


def cutoff_for(business_date):
    return datetime.combine(business_date + timedelta(days=1), time.min, TZ)


def text_field(value, label, max_length=2000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        raise ValidationError(f"{label}格式不正确或超过 {max_length} 字。")
    return value.strip()


def quantity(value, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10**12:
        raise ValidationError("数量必须为非负整数；未填写请使用空值。")
    return value


def display_version(value, fallback="未命名版本"):
    """Only remove a source model-year prefix; never use the label as identity."""
    value = str(value or "").strip()
    return re.sub(r"^20\d{2}款(?:\s*[·・:：/\-]\s*|\s*)", "", value).strip() or fallback


def catalog_name_key(product):
    """Match complete catalog labels without using them as permanent identity."""
    def normalize(value):
        return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()
    return tuple(normalize(display_version(product.get(key), "") if key == "version" else product.get(key))
                 for key in ("brand", "model", "version"))


class SOPStore:
    def __init__(self, db_path, sales_path, clock=now_local):
        self.db_path = Path(db_path)
        self.sales_path = Path(sales_path)
        self.clock = clock
        self.lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._schema()
        self.record_sales()

    def close(self):
        with self.lock:
            self.db.close()

    def _schema(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS product_catalog (
            id TEXT PRIMARY KEY, source_json TEXT NOT NULL, overrides_json TEXT NOT NULL,
            active INTEGER NOT NULL, source_present INTEGER NOT NULL,
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            start_month TEXT, end_month TEXT, source_generations_json TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS source_aliases (
            source_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, bound_at TEXT NOT NULL, owner TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS aliases_no_update BEFORE UPDATE ON source_aliases
          BEGIN SELECT RAISE(ABORT, 'Source bindings are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS aliases_no_delete BEFORE DELETE ON source_aliases
          BEGIN SELECT RAISE(ABORT, 'Source bindings are immutable'); END;
        CREATE TABLE IF NOT EXISTS catalog_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL,
            owner TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS catalog_no_update BEFORE UPDATE ON catalog_versions
          BEGIN SELECT RAISE(ABORT, 'Product catalog versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS catalog_no_delete BEFORE DELETE ON catalog_versions
          BEGIN SELECT RAISE(ABORT, 'Product catalog versions are immutable'); END;
        CREATE TABLE IF NOT EXISTS plans (
            product_id TEXT NOT NULL, month TEXT NOT NULL, forecast INTEGER, sop INTEGER,
            owner TEXT NOT NULL, reason TEXT NOT NULL, notes TEXT NOT NULL,
            revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(product_id, month));
        CREATE TABLE IF NOT EXISTS shipments (
            product_id TEXT NOT NULL, month TEXT NOT NULL, quantity INTEGER NOT NULL,
            source TEXT NOT NULL, status TEXT NOT NULL, batch TEXT NOT NULL,
            owner TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(product_id, month));
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, action TEXT NOT NULL,
            product_id TEXT NOT NULL, month TEXT NOT NULL, entity_key TEXT NOT NULL,
            before_json TEXT, after_json TEXT NOT NULL, owner TEXT NOT NULL,
            reason TEXT NOT NULL, notes TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_product_month ON events(product_id, month, id);
        CREATE INDEX IF NOT EXISTS events_cutoff ON events(recorded_at, id);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
          BEGIN SELECT RAISE(ABORT, 'Audit events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
          BEGIN SELECT RAISE(ABORT, 'Audit events are immutable'); END;
        CREATE TABLE IF NOT EXISTS sales_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL,
            recorded_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS sales_no_update BEFORE UPDATE ON sales_versions
          BEGIN SELECT RAISE(ABORT, 'Sales versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS sales_no_delete BEFORE DELETE ON sales_versions
          BEGIN SELECT RAISE(ABORT, 'Sales versions are immutable'); END;
        CREATE TABLE IF NOT EXISTS settings_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS settings_no_update BEFORE UPDATE ON settings_versions
          BEGIN SELECT RAISE(ABORT, 'Setting versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS settings_no_delete BEFORE DELETE ON settings_versions
          BEGIN SELECT RAISE(ABORT, 'Setting versions are immutable'); END;
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, business_date TEXT NOT NULL,
            cutoff_at TEXT NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL,
            label TEXT NOT NULL, plan_count INTEGER NOT NULL, payload TEXT NOT NULL,
            auto_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS generation_summary_history (generation TEXT PRIMARY KEY);
        CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON snapshots
          BEGIN SELECT RAISE(ABORT, 'Snapshots are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON snapshots
          BEGIN SELECT RAISE(ABORT, 'Snapshots are immutable'); END;
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(product_catalog)")}
        for name, definition in (("start_month", "TEXT"), ("end_month", "TEXT"), ("source_generations_json", "TEXT NOT NULL DEFAULT '[]'")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE product_catalog ADD COLUMN {name} {definition}")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("service_started_at", json.dumps(stamp(self.clock()))))
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("bridge_m6", "true"))
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("generation_mode", json.dumps("detail")))
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("zero_week_anchor", json.dumps("month_week_majority")))
        if not self.db.execute("SELECT 1 FROM settings_versions LIMIT 1").fetchone():
            self.db.execute("INSERT INTO settings_versions(recorded_at,payload) VALUES (?,?)", (stamp(self.clock()), json.dumps(self.settings())))

    def settings(self):
        return {row["key"]: json.loads(row["value"]) for row in self.db.execute("SELECT * FROM settings")}

    def update_settings(self, values):
        if not isinstance(values, dict):
            raise ValidationError("设置必须为对象。")
        updates = {}
        if "generation_mode" in values:
            if not isinstance(values["generation_mode"], str) or values["generation_mode"] not in {"detail", "summary"}:
                raise ValidationError("代际视图只能选择单代际或汇总代际。")
            updates["generation_mode"] = values["generation_mode"]
        if "bridge_m6" in values:
            if not isinstance(values["bridge_m6"], bool):
                raise ValidationError("bridge_m6 必须为布尔值。")
            updates["bridge_m6"] = values["bridge_m6"]
        if "zero_week_anchor" in values:
            anchor = values["zero_week_anchor"]
            if anchor not in {"previous_sunday", "month_week_majority"}:
                self._validate_sunday(anchor)
            updates["zero_week_anchor"] = anchor
        with self.lock:
            if "generation_mode" in updates:
                self._generation_rules()
            self.ensure_daily_snapshots()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for key, value in updates.items():
                    self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
                if updates:
                    self.db.execute("INSERT INTO settings_versions(recorded_at,payload) VALUES (?,?)", (stamp(self.clock()), json.dumps(self.settings())))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return {"ok": True, "settings": self.settings()}

    def _read_source_sales(self):
        if not self.sales_path.exists():
            return {"meta": {"warning": "销量文件尚未生成，请刷新销量。"}, "products": [], "sales": [], "sources": []}
        with self.sales_path.open("r", encoding="utf-8-sig") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or not isinstance(data.get("products"), list) or not isinstance(data.get("sales"), list):
            raise ValidationError("销量数据结构无效。")
        return data

    @staticmethod
    def _catalog_product(row):
        source = json.loads(row["source_json"])
        overrides = json.loads(row["overrides_json"])
        item = dict(source)
        item["version"] = display_version(source.get("version"), source.get("configuration") or "未命名版本")
        item.update({key: value for key, value in overrides.items() if key in {"brand", "model", "version"}})
        item["version"] = display_version(item.get("version"))
        item.update({"id": row["id"], "active": bool(row["active"]),
                     "source_backed": bool(source.get("id")), "source_present": bool(row["source_present"]),
                     "source_generation": source.get("generation") or source.get("model_year") or "",
                     "source_generations": json.loads(row["source_generations_json"]),
                     "source_version": source.get("source_version") or source.get("version") or "",
                     "start_month": row["start_month"], "end_month": row["end_month"],
                     "revision": row["revision"], "created_at": row["created_at"], "updated_at": row["updated_at"]})
        item["name"] = " ".join(str(item.get(k) or "") for k in ("brand", "model", "version")).strip()
        return item

    def _catalog_items(self):
        aliases = self.source_aliases()
        items = []
        for row in self.db.execute("SELECT * FROM product_catalog"):
            if row["id"] in aliases:
                continue
            item = self._catalog_product(row)
            source = json.loads(row["source_json"])
            item["source_ids"] = sorted({source["id"]} if source.get("id") else set())
            item["source_ids"] = sorted(set(item["source_ids"]) | {sid for sid, target in aliases.items() if target == row["id"]})
            generations = set(item["source_generations"])
            for sid in item["source_ids"]:
                linked = self.db.execute("SELECT source_generations_json FROM product_catalog WHERE id=?", (sid,)).fetchone()
                if linked:
                    generations.update(json.loads(linked["source_generations_json"]))
            item["source_generations"] = sorted(generations)
            items.append(item)
        counts = {}
        for item in items:
            name = catalog_name_key(item)
            counts[name] = counts.get(name, 0) + 1
        for item in items:
            item["name_conflict"] = counts[catalog_name_key(item)] > 1
        return sorted(items, key=lambda p: (brand_order_key(p.get("brand", "")), p.get("model", ""), p.get("version", ""), p["id"]))

    def source_aliases(self):
        with self.lock:
            return {row["source_id"]: row["target_id"] for row in self.db.execute("SELECT * FROM source_aliases ORDER BY bound_at,source_id")}

    def _known_summary_names(self, rules=None):
        rules = rules or self._generation_rules()
        members = {member for group in rules["groups"].values() for member in group}
        known = {row[0] for row in self.db.execute("SELECT generation FROM generation_summary_history")}
        return summary_names(rules) | (known - members)

    def _remap_sales(self, data, aliases):
        result = dict(data)
        known = self._known_summary_names() if aliases else set()
        excluded_aliases = {p["id"] for p in data.get("products", [])
                            if p["id"] in aliases and source_generations(p)
                            and all(g in known for g in source_generations(p))}
        def remap(rows):
            output, keys = [], set()
            for row in rows:
                if row["product_id"] in excluded_aliases:
                    continue
                item = {**row, "product_id": aliases.get(row["product_id"], row["product_id"])}
                key = (item["product_id"], item.get("month"), item.get("metric"))
                if key in keys:
                    raise ValidationError("关联后同一产品月份出现重复销量；请先核对来源，未合并数量。")
                keys.add(key)
                output.append(item)
            return output
        result["sales"] = remap(data.get("sales", []))
        if isinstance(data.get("version_reference"), dict):
            result["version_reference"] = {**data["version_reference"], "sales": remap(data["version_reference"].get("sales", []))}
        return result

    def _generation_rules(self):
        try:
            rules = load_merge_rules()
        except MergeRuleError as error:
            raise ValidationError(str(error)) from error
        self.db.executemany("INSERT OR IGNORE INTO generation_summary_history(generation) VALUES (?)",
                            [(name,) for name in summary_names(rules)])
        return rules

    def generation_view(self):
        with self.lock:
            rules = self._generation_rules()
            mode = self.settings().get("generation_mode", "detail")
            return {"mode": mode, "rule_signature": rules["signature"],
                    "signature": mode + ":" + rules["signature"], "config_path": rules["config_path"],
                    "derived": mode == "summary"}

    def _catalog_sales(self, data, include_excluded=False):
        catalog = self._catalog_items()
        if include_excluded:
            return {**data, "products": catalog}
        rules = self._generation_rules()
        known = {row[0] for row in self.db.execute("SELECT generation FROM generation_summary_history")}
        mode = self.settings().get("generation_mode", "detail")
        # Filter raw summary identities before aliases can mix them with real
        # versions. Both modes use single-generation values as their foundation.
        source, _ = project_generation_sources(data, "detail", rules, known)
        result = {**self._remap_sales(source, self.source_aliases()), "products": catalog}
        try:
            result, warnings = project_generation_sources(result, mode, rules, known)
        except MergeRuleError as error:
            raise ValidationError(str(error)) from error
        result["products"] = sorted(result["products"], key=lambda p: (brand_order_key(p.get("brand", "")),
                                    p.get("model", ""), p.get("version", ""), p["id"]))
        result["generation_view"] = {"mode": mode, "rule_signature": rules["signature"],
            "signature": mode + ":" + rules["signature"], "config_path": rules["config_path"],
            "warnings": warnings, "derived": mode == "summary"}
        meta = dict(result.get("meta", {}))
        coverage = dict(meta.get("metric_coverage", {}))
        locks = [r for r in result.get("sales", []) if r.get("metric") == "locked_orders"]
        model_locks = [r for r in result.get("model_sales", []) if r.get("metric") == "locked_orders"]
        def known_sum(rows):
            values = [r["value"] for r in rows if r.get("value") is not None]
            return sum(values) if values else None
        coverage["locked_orders"] = {**coverage.get("locked_orders", {}),
            "monthly_total": known_sum(locks), "model_monthly_total": known_sum(model_locks),
            "monthly_records": len(locks), "model_monthly_records": len(model_locks)}
        result["meta"] = {**meta, "metric_coverage": coverage}
        return result

    def view_plans(self, sales=None):
        """Return visible plans, deriving summary rows without persisting them."""
        with self.lock:
            sales = sales if sales is not None else self.read_sales()
            rows = [dict(row) for row in self.db.execute("SELECT * FROM plans ORDER BY product_id,month")]
            visible = {p["id"]: p for p in sales["products"]}
            result = [row for row in rows if row["product_id"] in visible and not visible[row["product_id"]].get("read_only")]
            for product in sales["products"]:
                if not product.get("read_only"):
                    continue
                members = set(product["member_product_ids"])
                months = sorted({row["month"] for row in rows if row["product_id"] in members})
                for month in months:
                    values = [row for row in rows if row["product_id"] in members and row["month"] == month]
                    item = {"product_id": product["id"], "month": month, "revision": 0,
                            "owner": "单代际自动汇总", "reason": "自动汇总", "notes": "", "read_only": True,
                            "updated_at": max((str(row.get("updated_at", "")) for row in values), default="")}
                    for field in ("sop", "forecast"):
                        known_values = [row[field] for row in values if row.get(field) is not None]
                        item[field] = sum(known_values) if known_values else None
                    item["known_members"] = sum(row.get("sop") is not None for row in values)
                    item["total_members"] = len(members)
                    item["partial"] = item["known_members"] < len(members) or bool(product.get("missing_member_generations"))
                    result.append(item)
            return result

    def _validate_model_aliases(self, data, aliases):
        """A model reference cannot disambiguate overlapping generations of one ID."""
        if not aliases:
            return
        known_summaries = self._known_summary_names()
        incoming = {p["id"]: p for p in data.get("products", [])}
        catalog = {row["id"]: row for row in self.db.execute("SELECT * FROM product_catalog")}
        for target in set(aliases.values()):
            source_ids = {target} | {sid for sid, tid in aliases.items() if tid == target}
            generations = set()
            for sid in source_ids:
                row = catalog.get(sid)
                if row:
                    generations.update(json.loads(row["source_generations_json"]))
                    previous_generation = json.loads(row["source_json"]).get("generation")
                    if previous_generation:
                        generations.add(previous_generation)
                generation = incoming.get(sid, {}).get("generation")
                if generation:
                    generations.add(generation)
            months = {}
            for row in data.get("model_sales", []):
                if row.get("generation") in known_summaries:
                    continue
                if row.get("generation") in generations and row.get("metric") == "locked_orders" and row.get("value") is not None:
                    months.setdefault(row["month"], set()).add(row["generation"])
            for month, overlap in months.items():
                if len(overlap) > 1:
                    raise ValidationError(f"来源关联存在冲突：{month} 同一产品的多个代际均有车型锁单数，无法确认是否重复；未合并，请先核对来源。")

    def validate_source_data(self, data):
        """Validate extracted data before a caller publishes a refreshed JSON file."""
        with self.lock:
            if not isinstance(data, dict) or not isinstance(data.get("products"), list) or not isinstance(data.get("sales"), list):
                raise ValidationError("销量数据结构无效。")
            seen = set()
            for product in data["products"]:
                if not isinstance(product, dict) or not isinstance(product.get("id"), str) or not product["id"] or product["id"] in seen:
                    raise ValidationError("销量产品编号无效或重复。")
                seen.add(product["id"])
            aliases = self.source_aliases()
            existing = {row["id"]: dict(row) for row in self.db.execute("SELECT * FROM product_catalog")}
            aliases = {**aliases, **self._auto_source_bindings(data, existing, aliases)}
            self._remap_sales(data, aliases)
            self._validate_model_aliases(data, aliases)

    def _record_catalog(self, owner, reason):
        self.db.execute("INSERT INTO catalog_versions(recorded_at,owner,reason,payload) VALUES (?,?,?,?)",
                        (stamp(self.clock()), owner, reason, json.dumps(self._catalog_items(), ensure_ascii=False)))

    def _ensure_catalog_fields(self):
        changed = False
        for row in self.db.execute("SELECT * FROM product_catalog").fetchall():
            source = json.loads(row["source_json"])
            generations = set(json.loads(row["source_generations_json"]))
            if source.get("generation"):
                generations.add(source["generation"])
            encoded = json.dumps(sorted(generations), ensure_ascii=False)
            # Missing source dates are not evidence of a launch date.
            initial_start = row["created_at"][:7] if row["id"].startswith("manual_") else "1900-01"
            overrides = json.loads(row["overrides_json"])
            start = row["start_month"] if row["start_month"] is not None or overrides.get("lifecycle_set") else initial_start
            end = row["end_month"]
            if not row["active"] and end is None:
                end = month_add(row["updated_at"][:7], -1)
            if (start, end, encoded) != (row["start_month"], row["end_month"], row["source_generations_json"]):
                self.db.execute("UPDATE product_catalog SET start_month=?,end_month=?,source_generations_json=? WHERE id=?", (start, end, encoded, row["id"]))
                changed = True
        return changed

    def _sync_catalog(self, data):
        """Discover by source ID, retain absentees, and never overwrite user labels."""
        source_products = {}
        for product in data["products"]:
            if not isinstance(product, dict) or not isinstance(product.get("id"), str) or not product["id"]:
                raise ValidationError("销量产品必须提供稳定的产品编号。")
            pid = product["id"]
            if pid in source_products:
                raise ValidationError("销量产品编号重复，未更新产品目录。")
            source_products[pid] = product
        existing = {row["id"]: dict(row) for row in self.db.execute("SELECT * FROM product_catalog")}
        aliases = self.source_aliases()
        alias_targets = set(aliases.values())
        self._remap_sales(data, aliases)  # Refuse overlapping inputs before any catalog mutation.
        self._validate_model_aliases(data, aliases)
        candidates = dict(source_products)
        # Upgrade older databases without abandoning products absent from today's source.
        if not self.db.execute("SELECT 1 FROM catalog_versions LIMIT 1").fetchone():
            for row in self.db.execute("SELECT payload FROM sales_versions ORDER BY id DESC"):
                for product in json.loads(row["payload"]).get("products", []):
                    candidates.setdefault(product["id"], product)
            for row in self.db.execute("SELECT product_id FROM plans UNION SELECT product_id FROM shipments"):
                if row["product_id"] not in candidates and row["product_id"] not in existing:
                    candidates[row["product_id"]] = {"id": row["product_id"], "brand": "历史产品", "model": row["product_id"], "version": "待确认版本"}
        # A newly observed source may complete a single, previously manual identity.
        # Multiple generations or already used source identities must remain distinct.
        auto_bindings = self._auto_source_bindings(data, existing, aliases)
        aliases = {**aliases, **auto_bindings}
        alias_targets = set(aliases.values())
        self._remap_sales(data, aliases)
        self._validate_model_aliases(data, aliases)
        changed = False
        current = stamp(self.clock())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for pid, source in candidates.items():
                encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                present = int(pid in source_products)
                before = existing.get(pid)
                if before is None:
                    self.db.execute("INSERT INTO product_catalog(id,source_json,overrides_json,active,source_present,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                    (pid, encoded, "{}", 1, present, 1, current, current))
                    changed = True
                elif pid not in alias_targets and (encoded != before["source_json"] or present != before["source_present"]):
                    self.db.execute("UPDATE product_catalog SET source_json=?,source_present=?,revision=revision+1,updated_at=? WHERE id=?",
                                    (encoded, present, current, pid))
                    changed = True
            for pid, before in existing.items():
                if pid not in source_products and pid not in alias_targets and before["source_present"]:
                    self.db.execute("UPDATE product_catalog SET source_present=0,revision=revision+1,updated_at=? WHERE id=?", (current, pid))
                    changed = True
            for source_id, target_id in auto_bindings.items():
                self.db.execute("INSERT INTO source_aliases VALUES (?,?,?,?)", (source_id, target_id, current, "来源自动匹配"))
                changed = True
            for target in alias_targets:
                source_ids = [sid for sid, tid in aliases.items() if tid == target]
                primary = source_ids[-1]
                linked = self.db.execute("SELECT * FROM product_catalog WHERE id=?", (primary,)).fetchone()
                target_row = self.db.execute("SELECT * FROM product_catalog WHERE id=?", (target,)).fetchone()
                present = int(target in source_products or any(sid in source_products for sid in source_ids))
                if linked and target_row and (target_row["source_json"] != linked["source_json"] or target_row["source_present"] != present):
                    self.db.execute("UPDATE product_catalog SET source_json=?,source_present=?,revision=revision+1,updated_at=? WHERE id=?",
                                    (linked["source_json"], present, current, target))
                    changed = True
            changed = self._ensure_catalog_fields() or changed
            if changed or not self.db.execute("SELECT 1 FROM catalog_versions LIMIT 1").fetchone():
                self._record_catalog("来源刷新", "更新来源产品目录；保留已知产品和人工名称")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _auto_source_bindings(self, data, existing, aliases):
        incoming = {}
        known_summaries = self._known_summary_names()
        def summary_only(product):
            generations = source_generations(product)
            return bool(generations) and all(g in known_summaries for g in generations)
        for product in data["products"]:
            if summary_only(product):
                continue
            incoming.setdefault(catalog_name_key(product), []).append(product)
        known = {}
        for product in self._catalog_items():
            if summary_only(product):
                continue
            known.setdefault(catalog_name_key(product), []).append(product)
        bindings = {}
        for key, sources in incoming.items():
            matches = known.get(key, [])
            if not all(key) or len(sources) != 1 or len(matches) != 1:
                continue
            source, target = sources[0], matches[0]
            source_id = source["id"]
            if (source_id in existing or source_id in aliases or target["source_backed"]
                    or target.get("source_ids") or not target["id"].startswith("manual_")):
                continue
            used = (self.db.execute("SELECT 1 FROM plans WHERE product_id=? LIMIT 1", (source_id,)).fetchone()
                    or self.db.execute("SELECT 1 FROM shipments WHERE product_id=? LIMIT 1", (source_id,)).fetchone()
                    or self.db.execute("SELECT 1 FROM events WHERE product_id=? LIMIT 1", (source_id,)).fetchone())
            if not used:
                bindings[source_id] = target["id"]
        return bindings

    def read_sales(self, *, include_excluded=False):
        with self.lock:
            data = self._read_source_sales()
            self._sync_catalog(data)
            return self._catalog_sales(data, include_excluded=include_excluded)

    def list_products(self):
        return {"items": self.read_sales()["products"]}

    def save_products(self, body):
        """Atomically create, rename or deactivate products, keeping IDs permanent."""
        if not isinstance(body, dict) or not isinstance(body.get("changes"), list) or not 1 <= len(body["changes"]) <= 2000:
            raise ValidationError("请提供 1 至 2000 条产品变更。")
        owner = text_field(body.get("owner") or "产品维护", "负责人", 100)
        reason = text_field(body.get("reason") or "维护车型与版本", "变更原因")
        with self.lock:
            self.ensure_daily_snapshots()
            self.read_sales()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                prepared, seen, conflicts = [], set(), []
                aliases = self.source_aliases()
                current = stamp(self.clock())
                for change in body["changes"]:
                    if not isinstance(change, dict):
                        raise ValidationError("产品变更格式无效。")
                    supplied_id = change.get("id")
                    if supplied_id is not None and (not isinstance(supplied_id, str) or not supplied_id):
                        raise ValidationError("产品编号格式无效。")
                    pid = supplied_id or "manual_" + uuid.uuid4().hex
                    if pid in seen:
                        raise ValidationError("同一次保存包含重复产品。")
                    seen.add(pid)
                    before = self.db.execute("SELECT * FROM product_catalog WHERE id=?", (pid,)).fetchone()
                    if pid in aliases:
                        raise ValidationError("该来源已关联到其他产品，请维护关联后的产品。")
                    if supplied_id and before is None:
                        raise ValidationError("产品不存在；新增产品请勿指定编号。")
                    revision = change.get("revision", 0)
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ValidationError("产品版本号必须为非负整数。")
                    if revision != (before["revision"] if before else 0):
                        conflicts.append({"id": pid, "current": self._catalog_product(before) if before else None})
                    prior = self._catalog_product(before) if before else {}
                    overrides = json.loads(before["overrides_json"]) if before else {}
                    for field, label in (("brand", "品牌"), ("model", "车型"), ("version", "版本名称")):
                        if field in change or not before:
                            value = text_field(change.get(field), label, 200)
                            if field == "version":
                                value = display_version(value, "")
                            if not value:
                                raise ValidationError(f"请填写{label}。")
                            if value != prior.get(field) or not before:
                                overrides[field] = value
                    active = change.get("active", prior.get("active", True))
                    if not isinstance(active, bool):
                        raise ValidationError("启用状态必须为布尔值。")
                    start_month = change.get("start_month", prior.get("start_month", current[:7]))
                    end_month = change.get("end_month", prior.get("end_month"))
                    if not active and prior.get("active", True):
                        end_month = month_add(current[:7], -1)
                    elif not active and end_month is None:
                        end_month = prior.get("end_month") or month_add(before["updated_at"][:7], -1)
                    elif active and prior.get("active") is False:
                        end_month = None
                    for value in (start_month, end_month):
                        if value is not None:
                            validate_month(value)
                    if active and start_month and end_month and end_month < start_month:
                        raise ValidationError("结束月份不能早于开始月份。")
                    if "start_month" in change or "end_month" in change:
                        overrides["lifecycle_set"] = True
                    binding = change.get("bind_source_id")
                    if binding is not None and (not isinstance(binding, str) or not binding):
                        raise ValidationError("关联来源编号无效。")
                    prepared.append((pid, before, overrides, active, binding, start_month, end_month))
                if conflicts:
                    raise ConflictError(conflicts)
                pending_bindings = {}
                for pid, before, overrides, active, binding, start_month, end_month in prepared:
                    if binding is None:
                        continue
                    source = self.db.execute("SELECT * FROM product_catalog WHERE id=?", (binding,)).fetchone()
                    if binding == pid or not source or not source["source_present"] or not json.loads(source["source_json"]).get("id"):
                        raise ValidationError("请选择本次销量来源中另一条有效来源记录。")
                    if binding in aliases or binding in aliases.values() or binding in pending_bindings or binding in seen or pid in pending_bindings:
                        raise ValidationError("来源已关联或本次请求存在交叉关联，请逐条核对。")
                    used = (self.db.execute("SELECT 1 FROM plans WHERE product_id=? LIMIT 1", (binding,)).fetchone()
                            or self.db.execute("SELECT 1 FROM shipments WHERE product_id=? LIMIT 1", (binding,)).fetchone()
                            or self.db.execute("SELECT 1 FROM events WHERE product_id=? AND entity IN ('plan','shipment') LIMIT 1", (binding,)).fetchone())
                    if used:
                        raise ValidationError("该来源已填写计划或出货记录，不能关联；请先核对，系统不会自动迁移数量。")
                    if before:
                        prior = self._catalog_product(before)
                        for field in ("brand", "model", "version"):
                            overrides.setdefault(field, prior[field])
                    pending_bindings[binding] = pid
                source_data = self._read_source_sales()
                combined_aliases = {**aliases, **pending_bindings}
                self._remap_sales(source_data, combined_aliases)
                self._validate_model_aliases(source_data, combined_aliases)
                saved_ids, changed = [], False
                for pid, before, overrides, active, binding, start_month, end_month in prepared:
                    encoded = json.dumps(overrides, ensure_ascii=False, sort_keys=True)
                    if before is None:
                        self.db.execute("INSERT INTO product_catalog(id,source_json,overrides_json,active,source_present,revision,created_at,updated_at,start_month,end_month) VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, "{}", encoded, int(active), 0, 1, current, current, start_month, end_month))
                        changed = True
                    elif overrides != json.loads(before["overrides_json"]) or active != bool(before["active"]) or (start_month, end_month) != (before["start_month"], before["end_month"]):
                        self.db.execute("UPDATE product_catalog SET overrides_json=?,active=?,start_month=?,end_month=?,revision=revision+1,updated_at=? WHERE id=?", (encoded, int(active), start_month, end_month, current, pid))
                        changed = True
                    if binding:
                        source = self.db.execute("SELECT * FROM product_catalog WHERE id=?", (binding,)).fetchone()
                        self.db.execute("INSERT INTO source_aliases VALUES (?,?,?,?)", (binding, pid, current, owner))
                        self.db.execute("UPDATE product_catalog SET source_json=?,source_present=1,revision=revision+1,updated_at=? WHERE id=?", (source["source_json"], current, pid))
                        changed = True
                    saved_ids.append(pid)
                changed = self._ensure_catalog_fields() or changed
                if changed:
                    self._record_catalog(owner, reason)
                saved = [p for p in self._catalog_items() if p["id"] in saved_ids]
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return {"ok": True, "items": saved, "saved_count": len(saved)}

    def record_sales(self):
        with self.lock:
            data = self._read_source_sales()
            self._sync_catalog(data)
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            previous = self.db.execute("SELECT digest FROM sales_versions ORDER BY id DESC LIMIT 1").fetchone()
            if not previous or previous["digest"] != digest:
                self.db.execute("INSERT INTO sales_versions(digest,recorded_at,payload) VALUES (?,?,?)", (digest, stamp(self.clock()), encoded))
            return self._catalog_sales(data)

    def state(self):
        with self.lock:
            self.ensure_daily_snapshots()
            data = self.record_sales()
            settings = self.settings()
            current = self.clock().astimezone(TZ)
            month = current.strftime("%Y-%m")
            end = horizon(month, settings["bridge_m6"])
            visible_ids = {p["id"] for p in data["products"]}
            data.update({
                "plans": self.view_plans(data),
                "shipments": [dict(row) for row in self.db.execute("SELECT * FROM shipments ORDER BY product_id,month,source,batch") if row["product_id"] in visible_ids],
                "settings": settings, "now": stamp(current), "current_month": month, "plan_end": end,
                "months": month_range(month, end),
                "history_months": [month_add(month, offset) for offset in (-3, -2, -1)],
                "snapshot_summary": self.snapshot_summary(),
            })
            return data

    def _changes(self, body, entity, products=None, allow_empty=False):
        if not isinstance(body, dict) or not isinstance(body.get("changes"), list) or not (0 if allow_empty else 1) <= len(body["changes"]) <= 20000:
            raise ValidationError("请提供 1 至 20000 条变更。")
        owner = text_field(body.get("owner"), "负责人", 100)
        if not owner:
            raise ValidationError("请填写负责人。")
        reason = text_field(body.get("reason"), "变更原因")
        notes = text_field(body.get("notes"), "备注", 5000)
        if products is None:
            products = {str(p["id"]): p for p in self.read_sales()["products"]}
        seen = set()
        result = []
        for change in body["changes"]:
            if not isinstance(change, dict):
                raise ValidationError("变更格式无效。")
            pid = change.get("product_id")
            if not isinstance(pid, str) or pid not in products:
                raise ValidationError("存在无法识别的产品。")
            if products[pid].get("read_only"):
                raise ValidationError("汇总代际为单代际自动相加，只读；请切换单代际后维护。")
            month = validate_month(change.get("month"))
            if entity == "plan":
                current_month = self.clock().astimezone(TZ).strftime("%Y-%m")
                product = products[pid]
                if not product.get("active", True) and month >= current_month:
                    raise ValidationError("已停用产品仅可修正历史月份；请先启用产品再维护当前或未来计划。")
                in_lifecycle = (not product.get("start_month") or month >= product["start_month"]) and (not product.get("end_month") or month <= product["end_month"])
                if not in_lifecycle:
                    existing_plan = self.db.execute("SELECT forecast,sop FROM plans WHERE product_id=? AND month=?", (pid, month)).fetchone()
                    historical_correction = month < current_month and existing_plan is not None and any(existing_plan[field] is not None for field in ("forecast", "sop"))
                    if not historical_correction:
                        raise ValidationError("该月份不在产品计划起止范围内；已有历史数量仍可修正。")
            revision = change.get("revision", 0)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ValidationError("版本号必须为非负整数。")
            row = {"product_id": pid, "month": month, "owner": owner, "revision": revision}
            if entity == "plan":
                if "forecast" not in change or "sop" not in change:
                    raise ValidationError("计划变更需同时提供 forecast 和 sop，未填写使用 null。")
                row.update(forecast=quantity(change["forecast"]), sop=quantity(change["sop"]),
                           reason=text_field(change.get("reason", reason), "变更原因"),
                           notes=text_field(change.get("notes", notes), "备注", 5000))
                key = (pid, month)
            else:
                source = change.get("source", "SP")
                status = change.get("status", "settled")
                if source not in {"SP", "IoC"} or status not in {"settled", "pending", "corrected"}:
                    raise ValidationError("实际出货来源或结算状态不正确。")
                batch = text_field(change.get("batch", ""), "批次", 100)
                row.update(quantity=quantity(change.get("quantity"), False), source=source, status=status, batch=batch)
                key = (pid, month)
            if key in seen:
                raise ValidationError("同一次保存包含重复记录。")
            seen.add(key)
            result.append((key, row))
        return result, owner, reason, notes

    def save_plans(self, body):
        return self._save(body, "plan")

    def _merge_workbook_catalog(self, changes, choice, current_stamp, current_month):
        """Merge existing row labels/status inside the caller's transaction.

        Stable IDs come from the trusted export, never from display names. An old
        unchanged label cannot undo a later web rename, and a deletion only stops
        future maintenance: its plan rows, source aliases and history remain.
        """
        updated, deactivated, seen, conflicts = set(), set(), set(), []
        aliases = self.source_aliases()
        allowed = {"id", "baseline", "baseline_plan_revisions", "brand", "model", "version", "active"}
        for change in changes:
            if not isinstance(change, dict) or set(change) - allowed:
                raise ValidationError("表格车型版本变更格式无效。")
            pid, baseline = change.get("id"), change.get("baseline")
            if not isinstance(pid, str) or not pid or pid in seen or not isinstance(baseline, dict):
                raise ValidationError("表格车型版本编号或基准无效，或同一版本重复出现。")
            seen.add(pid)
            row = self.db.execute("SELECT * FROM product_catalog WHERE id=?", (pid,)).fetchone()
            if row is None or pid in aliases:
                raise ValidationError("表格中的版本已合并或不在当前目录，请重新导出后修改。")
            prior = self._catalog_product(row)
            overrides = json.loads(row["overrides_json"])
            active, end_month = prior["active"], prior.get("end_month")
            fields = [field for field in ("brand", "model", "version", "active") if field in change]
            if not fields:
                raise ValidationError("车型版本变更未提供修改内容。")
            plan_revisions = change.get("baseline_plan_revisions")
            if plan_revisions is not None:
                if not isinstance(plan_revisions, dict) or len(plan_revisions) > 5000:
                    raise ValidationError("停用版本的计划修订基准无效。")
                for month, revision in plan_revisions.items():
                    validate_month(month)
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ValidationError("停用版本的计划修订基准无效。")
            for field in fields:
                if field not in baseline:
                    raise ValidationError("车型版本变更缺少原始字段基准，请重新导出。")
                original, requested, database = baseline[field], change[field], prior.get(field)
                if field == "active":
                    if type(original) is not bool or type(requested) is not bool:
                        raise ValidationError("车型版本启用状态及基准必须为布尔值。")
                else:
                    label = {"brand": "品牌", "model": "车型", "version": "版本名称"}[field]
                    original = text_field(original, label + "基准", 200)
                    requested = text_field(requested, label, 200)
                    if field == "version":
                        original, requested = display_version(original, ""), display_version(requested, "")
                    if not original or not requested:
                        raise ValidationError(f"请完整填写{label}；减少版本请删除整条明细行。")
                if requested == original or requested == database:
                    continue
                plan_changed = False
                if field == "active" and requested is False and plan_revisions is not None:
                    current_revisions = {r["month"]: r["revision"] for r in self.db.execute(
                        "SELECT month,revision FROM plans WHERE product_id=?", (pid,))}
                    plan_changed = current_revisions != plan_revisions
                if database != original or plan_changed:
                    conflict = {"kind": "catalog", "product_id": pid, "field": field,
                                "baseline": original, "workbook": requested, "database": database,
                                **{key: prior.get(key, "") for key in ("brand", "model", "version")}}
                    if plan_changed:
                        conflict["reason"] = "plans_changed"
                        conflict["message"] = "导出后该版本的计划已有更新，请核对是否仍需停用。"
                    conflicts.append(conflict)
                    if choice != "workbook":
                        continue
                if field == "active":
                    active = requested
                    if not active and prior["active"]:
                        end_month = month_add(current_month, -1)
                        deactivated.add(pid)
                    elif active and not prior["active"]:
                        end_month = None
                else:
                    overrides[field] = requested
            if overrides != json.loads(row["overrides_json"]) or active != prior["active"] or end_month != prior.get("end_month"):
                self.db.execute("""UPDATE product_catalog SET overrides_json=?,active=?,end_month=?,
                    revision=revision+1,updated_at=? WHERE id=?""",
                    (json.dumps(overrides, ensure_ascii=False, sort_keys=True), int(active), end_month, current_stamp, pid))
                updated.add(pid)
        if conflicts and choice is None:
            raise ConflictError(conflicts)
        return updated, deactivated

    def import_workbook_plans(self, body):
        """Import known cells and named entry rows as one catalog/plan transaction.

        Blank entry cells do not clear existing plans. Entry quantities use the same
        three-way baseline rule as the managed worksheet; zero is a real quantity.
        """
        if not isinstance(body, dict):
            raise ValidationError("表格导入格式无效。")
        entries, changes = body.get("entries", []), body.get("changes", [])
        catalog_changes = body.get("catalog_changes", [])
        entry_tracking_token = body.get("entry_tracking_token")
        if entry_tracking_token is not None and (not isinstance(entry_tracking_token, str)
                or not re.fullmatch(r"[0-9a-f]{32}", entry_tracking_token)):
            raise ValidationError("新增版本的导入跟踪标识无效，请重新导出。")
        if (not isinstance(entries, list) or len(entries) > 2000 or not isinstance(changes, list) or len(changes) > 20000
                or not isinstance(catalog_changes, list) or len(catalog_changes) > 2000):
            raise ValidationError("表格导入记录过多或格式无效。")
        choice = body.get("choice")
        if choice not in {None, "db", "workbook"}:
            raise ValidationError("表格冲突处理选项无效。")
        owner = text_field(body.get("owner") or "Excel计划表", "负责人", 100)
        reason = text_field(body.get("reason") or "表格同步", "变更原因")
        notes = text_field(body.get("notes"), "备注", 5000)
        with self.lock:
            self.ensure_daily_snapshots()
            self.read_sales()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                current_stamp = stamp(self.clock())
                current_month = self.clock().astimezone(TZ).strftime("%Y-%m")
                plan_end = horizon(current_month, self.settings()["bridge_m6"])
                catalog_updated, deactivated = self._merge_workbook_catalog(
                    catalog_changes, choice, current_stamp, current_month)
                names = {}
                for product in self._catalog_items():
                    names.setdefault(catalog_name_key(product), []).append(product)
                created, resolved, proposed = set(), [], {}
                for index, entry in enumerate(entries):
                    if not isinstance(entry, dict) or not isinstance(entry.get("plans", []), list):
                        raise ValidationError("新增车型/版本行格式无效。")
                    labels = {}
                    for field, label in (("brand", "品牌"), ("model", "车型"), ("version", "版本名称")):
                        value = text_field(entry.get(field), label, 200)
                        if field == "version":
                            value = display_version(value, "")
                        if not value:
                            raise ValidationError(f"新增车型/版本行请填写{label}。")
                        labels[field] = value
                    name_key = catalog_name_key(labels)
                    matches = names.get(name_key, [])
                    if len(matches) > 1:
                        raise ValidationError("相同品牌、车型和版本对应多个来源代际，请在已有明细行填写，不能自动合并。")
                    entry_plans = []
                    for plan in entry.get("plans", []):
                        if not isinstance(plan, dict):
                            raise ValidationError("新增版本的计划量格式无效。")
                        month = validate_month(plan.get("month"))
                        if month > plan_end:
                            raise ValidationError(f"{month}已超出当前开放计划范围。")
                        value = quantity(plan.get("sop"))
                        baseline = quantity(plan.get("baseline_sop"))
                        if value is not None or baseline is not None:
                            entry_plans.append((month, value, baseline))
                    start = min([current_month] + [month for month, _, _ in entry_plans])
                    if not matches:
                        pid = "manual_" + uuid.uuid4().hex
                        overrides = {**labels, "lifecycle_set": True}
                        self.db.execute("""INSERT INTO product_catalog
                            (id,source_json,overrides_json,active,source_present,revision,created_at,updated_at,start_month)
                            VALUES (?,?,?,?,?,?,?,?,?)""", (pid, "{}", json.dumps(overrides, ensure_ascii=False), 1, 0, 1, current_stamp, current_stamp, start))
                        created.add(pid)
                        product = self._catalog_product(self.db.execute("SELECT * FROM product_catalog WHERE id=?", (pid,)).fetchone())
                        names[name_key] = [product]
                    else:
                        product = matches[0]
                        pid = product["id"]
                        if pid in created and start < product["start_month"]:
                            self.db.execute("UPDATE product_catalog SET start_month=? WHERE id=?", (start, pid))
                            product["start_month"] = start
                    resolved.append(pid)
                    for month, value, baseline in entry_plans:
                        key = (pid, month)
                        if key in proposed and (value, baseline) != proposed[key][:2]:
                            raise ValidationError("重复填写的同一车型版本月份计划量或基准不一致，请合并为一行。")
                        proposed[key] = (value, baseline, index)
                if len(changes) + len(proposed) > 20000:
                    raise ValidationError("同一次表格导入最多支持 20000 条计划变更。")
                ordinary = {}
                for change in changes:
                    if not isinstance(change, dict):
                        raise ValidationError("计划变更格式无效。")
                    key = (change.get("product_id"), change.get("month"))
                    if key in ordinary:
                        raise ValidationError("同一次保存包含重复记录。")
                    if key[0] in deactivated:
                        raise ValidationError("同一次导入不能同时删除版本行并修改该版本计划，请分开处理。")
                    ordinary[key] = change
                combined, conflicts = list(changes), []
                for (pid, month), (value, baseline, index) in proposed.items():
                    if pid in deactivated:
                        raise ValidationError("同一次导入不能同时删除版本行并修改该版本计划，请分开处理。")
                    key = (pid, month)
                    if key in ordinary:
                        if ordinary[key].get("sop") != value:
                            raise ValidationError("新增区与已有明细行填写了不同的同一月份计划量，请先统一。")
                        continue
                    row = self.db.execute("SELECT * FROM plans WHERE product_id=? AND month=?", key).fetchone()
                    previous = dict(row) if row else {}
                    database_value = previous.get("sop")
                    if value == baseline or value == database_value:
                        continue
                    if database_value != baseline:
                        conflicts.append({"product_id": pid, "month": month, "baseline": baseline,
                                          "workbook": value, "database": database_value,
                                          "current": previous or None, "manual_entry": index})
                        if choice != "workbook":
                            continue
                    combined.append({"product_id": pid, "month": month, "sop": value,
                                     "forecast": previous.get("forecast"), "revision": previous.get("revision", 0),
                                     "notes": previous.get("notes", notes)})
                if conflicts and choice is None:
                    raise ConflictError(conflicts)
                catalog = {p["id"]: p for p in self._catalog_items()}
                prepared, owner, reason, notes = self._changes(
                    {"changes": combined, "owner": owner, "reason": reason, "notes": notes},
                    "plan", products=catalog, allow_empty=True)
                saved = self._apply_changes(prepared, "plan", owner, reason, notes)
                if created or catalog_updated:
                    self._record_catalog(owner, "表格维护车型与版本")
                products = [catalog[pid] for pid in resolved]
                if entry_tracking_token is not None:
                    # Downloads keep no server-writable file after import. Remember
                    # newly assigned IDs in the same transaction, so a later old
                    # copy cannot silently create a second ID for that entry.
                    exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_download_entry_imports'").fetchone()
                    if not exists:
                        raise ValidationError("导出表的新增版本跟踪尚未就绪，请刷新后重试。")
                    tracked_row = self.db.execute("SELECT payload FROM plan_download_entry_imports WHERE token=?",
                                                  (entry_tracking_token,)).fetchone()
                    try:
                        tracked = json.loads(tracked_row["payload"]) if tracked_row else {}
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ValidationError("新增版本的导入跟踪记录无效，请重新导出。") from error
                    if not isinstance(tracked, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in tracked.items()):
                        raise ValidationError("新增版本的导入跟踪记录无效，请重新导出。")
                    for entry, pid in zip(entries, resolved):
                        name = json.dumps(catalog_name_key(entry), ensure_ascii=False, separators=(",", ":"))
                        if name in tracked and tracked[name] != pid:
                            raise ValidationError("这个旧文件中的新增版本已登记或改名，请重新导出最新表格后修改。")
                        tracked[name] = pid
                    self.db.execute("INSERT OR REPLACE INTO plan_download_entry_imports(token,payload) VALUES (?,?)",
                                    (entry_tracking_token, json.dumps(tracked, ensure_ascii=False, sort_keys=True)))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return {"ok": True, "items": saved, "saved_count": len(saved),
                    "products": products, "created_count": len(created),
                    "catalog_updated_count": len(catalog_updated), "deactivated_count": len(deactivated)}

    def save_shipments(self, body):
        return self._save(body, "shipment")

    def _save(self, body, entity):
        with self.lock:
            # Seal elapsed cutoffs before accepting any new mutations.
            self.ensure_daily_snapshots()
            changes, owner, reason, notes = self._changes(body, entity)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                saved = self._apply_changes(changes, entity, owner, reason, notes)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return {"ok": True, "items": saved, "saved_count": len(saved)}

    def _apply_changes(self, changes, entity, owner, reason, notes):
        """Write validated rows inside the caller's transaction, with normal audit events."""
        table = "plans" if entity == "plan" else "shipments"
        prepared, conflicts = [], []
        for key, row in changes:
            previous = self.db.execute(f"SELECT * FROM {table} WHERE product_id=? AND month=?", key).fetchone()
            before = dict(previous) if previous else None
            if row["revision"] != (before["revision"] if before else 0):
                conflicts.append({"product_id": row["product_id"], "month": row["month"], "current": before})
            prepared.append((key, row, before))
        if conflicts:
            raise ConflictError(conflicts)
        recorded_at, saved = stamp(self.clock()), []
        for key, row, before in prepared:
            row["revision"] += 1
            row["updated_at"] = recorded_at
            columns = list(row)
            self.db.execute(f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [row[column] for column in columns])
            self.db.execute("""INSERT INTO events(entity,action,product_id,month,entity_key,before_json,after_json,owner,reason,notes,recorded_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                entity, "update" if before else "create", row["product_id"], row["month"], json.dumps(key),
                json.dumps(before, ensure_ascii=False) if before else None,
                json.dumps(row, ensure_ascii=False), owner, row.get("reason", reason), row.get("notes", notes), recorded_at))
            saved.append(row)
        return saved

    def history(self, product_id=None, month=None):
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if product_id:
            sql += " AND product_id=?"
            args.append(product_id)
        if month:
            sql += " AND month=?"
            args.append(validate_month(month))
        sql += " ORDER BY id DESC LIMIT 2000"
        with self.lock:
            records = []
            for row in self.db.execute(sql, args):
                item = dict(row)
                item["before"] = json.loads(item.pop("before_json")) if item["before_json"] else None
                item.pop("before_json", None)
                item["after"] = json.loads(item.pop("after_json"))
                item["entity_key"] = json.loads(item["entity_key"])
                records.append(item)
            return {"items": records}

    def _snapshot(self, cutoff, business_date, kind, label, auto_key=None, inclusive=False):
        cutoff_text = stamp(cutoff)
        operator = "<=" if inclusive else "<"
        rows = self.db.execute(f"SELECT * FROM events WHERE recorded_at {operator} ? ORDER BY id", (cutoff_text,)).fetchall()
        state = {"plan": {}, "shipment": {}}
        for row in rows:
            state[row["entity"]][row["entity_key"]] = json.loads(row["after_json"])
        sales = self.db.execute(f"SELECT * FROM sales_versions WHERE recorded_at {operator} ? ORDER BY recorded_at DESC,id DESC LIMIT 1", (cutoff_text,)).fetchone()
        payload = json.loads(sales["payload"]) if sales else {"products": [], "sales": [], "meta": {}, "sources": []}
        catalog = self.db.execute(f"SELECT id,payload FROM catalog_versions WHERE recorded_at {operator} ? ORDER BY recorded_at DESC,id DESC LIMIT 1", (cutoff_text,)).fetchone()
        if catalog:
            catalog_products = json.loads(catalog["payload"])
            aliases = {sid: p["id"] for p in catalog_products for sid in p.get("source_ids", []) if sid != p["id"]}
            # Remap while the original source IDs/generations are still
            # available, before replacing them with the frozen catalog view.
            payload = self._remap_sales(payload, aliases)
            payload["products"] = catalog_products
        settings_row = self.db.execute(f"SELECT payload FROM settings_versions WHERE recorded_at {operator} ? ORDER BY recorded_at DESC,id DESC LIMIT 1", (cutoff_text,)).fetchone()
        settings = json.loads(settings_row["payload"]) if settings_row else {"bridge_m6": True, "zero_week_anchor": "month_week_majority"}
        snap_month = business_date.strftime("%Y-%m")
        payload.update({"plans": list(state["plan"].values()), "shipments": list(state["shipment"].values()), "settings": settings})
        payload["meta"] = {**payload.get("meta", {}), "snapshot_cutoff_at": cutoff_text,
                           "history_event_id": rows[-1]["id"] if rows else None,
                           "sales_version_id": sales["id"] if sales else None,
                           "catalog_version_id": catalog["id"] if catalog else None,
                           "months": month_range(snap_month, horizon(snap_month, settings["bridge_m6"])),
                           "sales_available_at_cutoff": sales is not None}
        created = stamp(self.clock())
        cursor = self.db.execute("INSERT INTO snapshots(business_date,cutoff_at,created_at,kind,label,plan_count,payload,auto_key) VALUES (?,?,?,?,?,?,?,?)", (
            business_date.isoformat(), cutoff_text, created, kind, label, len(payload["plans"]), json.dumps(payload, ensure_ascii=False), auto_key))
        return self.get_snapshot(cursor.lastrowid)

    def ensure_daily_snapshots(self):
        with self.lock:
            current = self.clock().astimezone(TZ)
            started = datetime.fromisoformat(self.settings()["service_started_at"])
            cutoff = datetime.combine(started.date() + timedelta(days=1), time.min, TZ)
            # Only real elapsed cutoffs since installation; audit events provide historical truth.
            while cutoff <= current:
                key = stamp(cutoff)
                if not self.db.execute("SELECT 1 FROM snapshots WHERE auto_key=?", (key,)).fetchone():
                    business_date = cutoff.date() - timedelta(days=1)
                    kind = "sunday" if business_date.weekday() == 6 else "daily"
                    self._snapshot(cutoff, business_date, kind, f"{business_date.isoformat()} 24:00", key)
                cutoff += timedelta(days=1)

    def manual_snapshot(self, label=""):
        with self.lock:
            self.ensure_daily_snapshots()
            self.record_sales()
            current = self.clock().astimezone(TZ)
            # Manual snapshots include all completed writes up to the actual capture time.
            return {"ok": True, **self._snapshot(current, current.date(), "manual", text_field(label, "快照名称", 200) or "手动快照", inclusive=True)}

    def snapshots(self):
        with self.lock:
            self.ensure_daily_snapshots()
            return {"items": [dict(row) for row in self.db.execute("SELECT id,business_date,cutoff_at,created_at,kind,label,plan_count FROM snapshots ORDER BY cutoff_at DESC,id DESC")]}

    def snapshot_summary(self):
        items = self.snapshots()["items"]
        return {"total": len(items), "sunday_count": sum(i["kind"] == "sunday" for i in items),
                "latest": items[0] if items else None, "service_started_at": self.settings()["service_started_at"]}

    def get_snapshot(self, snapshot_id):
        try:
            snapshot_id = int(snapshot_id)
        except (TypeError, ValueError):
            raise ValidationError("快照编号无效。")
        with self.lock:
            row = self.db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if not row:
                raise ValidationError("快照不存在。")
            item = dict(row)
            payload = json.loads(item.pop("payload"))
            item.pop("auto_key", None)
            return {"snapshot": item, **payload}

    def compare(self, left, right):
        a, b = self.get_snapshot(left), self.get_snapshot(right)
        before = {(r["product_id"], r["month"]): r for r in a["plans"]}
        after = {(r["product_id"], r["month"]): r for r in b["plans"]}
        items = []
        for pid, month in sorted(before.keys() | after.keys()):
            aa, bb = before.get((pid, month), {}), after.get((pid, month), {})
            if all(aa.get(field) == bb.get(field) for field in ("forecast", "sop", "owner", "reason", "notes")):
                continue
            item = {"product_id": pid, "month": month}
            for field in ("forecast", "sop", "owner", "reason", "notes", "revision"):
                item[f"{field}_before"] = aa.get(field)
                item[f"{field}_after"] = bb.get(field)
            items.append(item)
        return {"items": items, "left": a["snapshot"], "right": b["snapshot"]}

    @staticmethod
    def _validate_sunday(value):
        try:
            anchor = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValidationError("0 周锚点必须为 YYYY-MM-DD 的周日日期。")
        if anchor.weekday() != 6:
            raise ValidationError("0 周锚点必须为周日。")
        return anchor

    def matching(self, month, brand="", model="", product_id="", zero_week_date=""):
        validate_month(month)
        with self.lock:
            self.ensure_daily_snapshots()
            sales = self.read_sales()
            existing_ids = {row["product_id"] for row in self.db.execute("SELECT product_id FROM plans WHERE month=? AND (forecast IS NOT NULL OR sop IS NOT NULL) UNION SELECT product_id FROM shipments WHERE month=?", (month, month))}
            products = [p for p in sales["products"] if (not brand or p.get("brand") == brand)
                        and (not model or p.get("model") == model) and (not product_id or p["id"] == product_id)
                        and (p["id"] in existing_ids or ((not p.get("start_month") or month >= p["start_month"])
                             and (not p.get("end_month") or month <= p["end_month"])))]
            product_ids = {pid for p in products for pid in p.get("member_product_ids", [p["id"]])}
            incomplete_groups = any(p.get("missing_member_generations") for p in products)
            if not products:
                raise ValidationError("没有符合筛选条件的产品。")
            configured = zero_week_date or self.settings().get("zero_week_anchor", "month_week_majority")
            if configured == "month_week_majority":
                zero = week_zero_for_month(month)
            elif configured == "previous_sunday":
                zero = sunday_before(month)
            else:
                zero = self._validate_sunday(configured)
            shipment_rows = [dict(r) for r in self.db.execute("SELECT * FROM shipments WHERE month=?", (month,)) if r["product_id"] in product_ids]
            covered = {r["product_id"] for r in shipment_rows}
            actual = sum(r["quantity"] for r in shipment_rows) if covered == product_ids and not incomplete_groups else None
            current_month = self.clock().astimezone(TZ).strftime("%Y-%m")
            actual_status = "missing" if actual is None else "pending" if any(r["status"] == "pending" for r in shipment_rows) else "in_progress" if month >= current_month else "settled"
            rows = []
            for week in range(13, -1, -1):
                day = zero - timedelta(weeks=week)
                cutoff = cutoff_for(day)
                snap = self.db.execute("SELECT id,payload FROM snapshots WHERE auto_key=?", (stamp(cutoff),)).fetchone()
                plan = None
                if snap:
                    candidates = [r for r in json.loads(snap["payload"])["plans"] if r["month"] == month and r["product_id"] in product_ids]
                    if {r["product_id"] for r in candidates if r["sop"] is not None} == product_ids and not incomplete_groups:
                        plan = sum(r["sop"] for r in candidates)
                match = None
                if not snap:
                    status = "future" if cutoff > self.clock() else "missing_snapshot"
                elif plan is None:
                    status = "missing_plan"
                elif actual_status != "settled":
                    status = actual_status
                elif actual == 0 and plan == 0:
                    status = "undefined_zero"
                else:
                    match = min(plan, actual) / max(plan, actual) * 100
                    status = "complete"
                rows.append({"week": week, "business_date": day.isoformat(), "cutoff_at": stamp(cutoff),
                             "snapshot_id": snap["id"] if snap else None, "plan": plan, "actual": actual,
                             "match": match, "weight": WEIGHTS[week],
                             "contribution": match * WEIGHTS[week] if match is not None else None, "status": status})
            complete = sum(r["plan"] is not None for r in rows)
            scored = sum(r["status"] == "complete" for r in rows)
            official = scored == 14 and actual_status == "settled"
            return {"month": month, "zero_week": zero.isoformat(), "actual": actual, "actual_status": actual_status,
                    "complete_weeks": complete, "scored_weeks": scored,
                    "score": sum(r["contribution"] for r in rows) if official else None,
                    "status": "official" if official else "incomplete", "rows": rows,
                    "product_count": len(products), "weight_total": sum(WEIGHTS.values()),
                    "formula": "min(actual, SOP) / max(actual, SOP) * 100", "official": official}
