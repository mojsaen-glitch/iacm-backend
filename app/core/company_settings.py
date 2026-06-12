"""Per-company OPERATIONAL settings on top of the existing `settings` table
(key/value/company_id) — batch 1 of the company-settings plan
(docs/COMPANY_SETTINGS_PLAN.md).

DESIGN GUARANTEES
  • No row ⇒ behaviour is EXACTLY today's: every default below is either
    derived programmatically from the existing constants (fleet templates) or
    a literal copy pinned by parity tests.
  • Reads NEVER fail a caller: any missing/broken value falls open to the
    default (a corrupt settings row must not take down a safety gate).
  • Validation runs on WRITE (the endpoint), strict per key.
  • NOTHING consumes these yet — gate wiring comes in later batches.
"""
from __future__ import annotations

import json
import logging
import time

from app.core.fleet_complement import (
    _FLEET, _GENERIC, _OPERATIONAL, _OPERATIONAL_GENERIC,
)

_log = logging.getLogger(__name__)

# ── Defaults (no-row ⇒ today's behaviour, verbatim) ───────────────────────────

def _complement_defaults() -> dict:
    """Derived from fleet_complement._FLEET — parity by construction."""
    def row(spec):
        mp, xp, mc, xc, eng = spec
        return {"min_pilots": mp, "max_pilots": xp,
                "min_cabin": mc, "max_cabin": xc, "engineers": eng}
    out = {t: row(s) for t, s in _FLEET.items()}
    out["_generic"] = row(_GENERIC)
    return out


def _operational_defaults() -> dict:
    """Derived from fleet_complement._OPERATIONAL — parity by construction."""
    out = {t: dict(s) for t, s in _OPERATIONAL.items()}
    out["_generic"] = dict(_OPERATIONAL_GENERIC)
    return out


# Literal copies below are pinned to their source constants by parity tests
# (tests/test_operational_settings.py) — they cannot drift silently.
DEFAULTS: dict = {
    # أ — قوالب الطاقم
    "ops.fleet.complement": _complement_defaults(),
    "ops.fleet.augment_threshold_hours": 8,
    "ops.fleet.operational_complement": _operational_defaults(),
    # ب — أكواد الأسباب
    "ops.delay.reason_codes": [
        "weather", "technical", "crew_shortage", "operational",
        "commercial", "atc", "security", "other",
    ],
    "ops.aircraft_change.reason_codes": [
        "maintenance", "aog", "capacity", "swap", "operational", "other",
    ],
    "ops.replace.reasons": [
        "رفض التكليف", "مرض / إجازة", "ضرورة تشغيلية", "تعارض جدول", "سبب آخر",
    ],
    "ops.admin_confirm.reasons": [
        "موافقة هاتفية", "موافقة واتساب", "ضرورة تشغيلية",
        "تأكيد من المشرف", "سبب آخر",
    ],
    # ج — عتبات العرض
    "ops.ui.low_hours_threshold": 40,
    # د — المطارات المحلية (wiring deferred to the LAST batch — sensitive)
    "ops.airports.domestic": ["BGW", "NJF", "BSR", "EBL", "OSM", "ISU", "RUM", "TQD"],
    # هـ — افتراضيات FTL العامة (قيم فقط — ليست محرك FTL العالمي)
    "ftl.max_monthly_hours": 100.0,
    "ftl.max_28day_hours": 100.0,
    "ftl.max_yearly_hours": 900.0,
    "ftl.min_rest_domestic_hours": 10.0,
    "ftl.min_rest_international_hours": 12.0,
    "ftl.default_max_fdp_minutes": 780,
    # و — إيقاعات تشغيلية
    "ops.acceptance_reminders": {
        "gentle_hours": 48, "urgent_hours": 6,
        "gentle_cooldown_hours": 24, "followup_cooldown_hours": 6,
    },
    "ops.boarding_lead_minutes": 30,
    # ز — تغذية AutoAssign (استشاري)
    "ops.autoassign.ftl_limits": {
        "max_monthly_flights": 30, "max_monthly_hours": 100.0,
        "max_daily_duty_hours": 14.0,
        "min_rest_domestic_hours": 10.0, "min_rest_international_hours": 12.0,
    },
}

KNOWN_KEYS = frozenset(DEFAULTS)


# ── Validation (strict, WRITE-side only) ─────────────────────────────────────

def _err(msg: str):
    raise ValueError(msg)


def _require_number(v, name, minimum=0):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _err(f"{name}: يجب أن يكون رقماً")
    if v < minimum:
        _err(f"{name}: لا يقبل قيمة أقل من {minimum}")


def _require_str_list(v, name):
    if not isinstance(v, list) or not v:
        _err(f"{name}: قائمة غير فارغة مطلوبة")
    seen = set()
    for item in v:
        if not isinstance(item, str) or not item.strip():
            _err(f"{name}: كل عنصر يجب أن يكون نصاً غير فارغ")
        if item in seen:
            _err(f"{name}: قيمة مكررة ({item})")
        seen.add(item)


def _validate_complement(v):
    if not isinstance(v, dict) or not v:
        _err("complement: خريطة طراز→قالب مطلوبة")
    fields = ("min_pilots", "max_pilots", "min_cabin", "max_cabin", "engineers")
    for ac_type, spec in v.items():
        if not isinstance(ac_type, str) or not ac_type.strip():
            _err("complement: اسم طراز غير صالح")
        if not isinstance(spec, dict) or set(spec) != set(fields):
            _err(f"complement[{ac_type}]: الحقول المطلوبة {fields}")
        for f in fields:
            if isinstance(spec[f], bool) or not isinstance(spec[f], int) or spec[f] < 0:
                _err(f"complement[{ac_type}].{f}: عدد صحيح ≥ 0 مطلوب")
        if spec["min_pilots"] > spec["max_pilots"]:
            _err(f"complement[{ac_type}]: min_pilots أكبر من max_pilots")
        if spec["min_cabin"] > spec["max_cabin"]:
            _err(f"complement[{ac_type}]: min_cabin أكبر من max_cabin")


def _validate_operational(v):
    keys = {"ame", "lsh", "ifso", "obs", "us", "tech"}
    if not isinstance(v, dict) or not v:
        _err("operational_complement: خريطة طراز→قالب مطلوبة")
    for ac_type, spec in v.items():
        if not isinstance(spec, dict) or not set(spec) <= keys:
            _err(f"operational_complement[{ac_type}]: المفاتيح المسموحة {sorted(keys)}")
        for k, n in spec.items():
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                _err(f"operational_complement[{ac_type}].{k}: عدد صحيح ≥ 0 مطلوب")


def _validate_dict_numbers(v, name, required_keys, minimum=0):
    if not isinstance(v, dict) or set(v) != set(required_keys):
        _err(f"{name}: الحقول المطلوبة {sorted(required_keys)}")
    for k, n in v.items():
        _require_number(n, f"{name}.{k}", minimum)


_VALIDATORS = {
    "ops.fleet.complement": _validate_complement,
    "ops.fleet.augment_threshold_hours":
        lambda v: _require_number(v, "augment_threshold_hours", minimum=1),
    "ops.fleet.operational_complement": _validate_operational,
    "ops.delay.reason_codes":
        lambda v: _require_str_list(v, "delay.reason_codes"),
    "ops.aircraft_change.reason_codes":
        lambda v: _require_str_list(v, "aircraft_change.reason_codes"),
    "ops.replace.reasons":
        lambda v: _require_str_list(v, "replace.reasons"),
    "ops.admin_confirm.reasons":
        lambda v: _require_str_list(v, "admin_confirm.reasons"),
    "ops.ui.low_hours_threshold":
        lambda v: _require_number(v, "low_hours_threshold"),
    "ops.airports.domestic":
        lambda v: _require_str_list(v, "airports.domestic"),
    "ftl.max_monthly_hours":
        lambda v: _require_number(v, "max_monthly_hours", minimum=1),
    "ftl.max_28day_hours":
        lambda v: _require_number(v, "max_28day_hours", minimum=1),
    "ftl.max_yearly_hours":
        lambda v: _require_number(v, "max_yearly_hours", minimum=1),
    "ftl.min_rest_domestic_hours":
        lambda v: _require_number(v, "min_rest_domestic_hours", minimum=1),
    "ftl.min_rest_international_hours":
        lambda v: _require_number(v, "min_rest_international_hours", minimum=1),
    "ftl.default_max_fdp_minutes":
        lambda v: _require_number(v, "default_max_fdp_minutes", minimum=60),
    "ops.acceptance_reminders":
        lambda v: _validate_dict_numbers(
            v, "acceptance_reminders",
            ("gentle_hours", "urgent_hours",
             "gentle_cooldown_hours", "followup_cooldown_hours"), minimum=1),
    "ops.boarding_lead_minutes":
        lambda v: _require_number(v, "boarding_lead_minutes"),
    "ops.autoassign.ftl_limits":
        lambda v: _validate_dict_numbers(
            v, "autoassign.ftl_limits",
            ("max_monthly_flights", "max_monthly_hours", "max_daily_duty_hours",
             "min_rest_domestic_hours", "min_rest_international_hours"),
            minimum=1),
}


def validate_setting(key: str, value) -> None:
    """Raises ValueError (Arabic message) when the value is invalid."""
    if key not in KNOWN_KEYS:
        raise ValueError(f"مفتاح غير معروف: {key}")
    _VALIDATORS[key](value)


# ── Loader + short cache (TTL 60s, fail-open to defaults) ────────────────────

_CACHE: dict = {}            # (company_id, key) -> (expires_monotonic, value)
_CACHE_TTL = 60.0


def invalidate_settings_cache(company_id: str | None = None) -> None:
    if company_id is None:
        _CACHE.clear()
        return
    for k in [k for k in _CACHE if k[0] == company_id]:
        _CACHE.pop(k, None)


def get_company_setting(sb, company_id: str, key: str, default=None):
    """Effective value for (company, key): the stored override when present and
    parseable, else the code default. NEVER raises — a broken row fails open."""
    if default is None:
        default = DEFAULTS.get(key)
    hit = _CACHE.get((company_id, key))
    if hit and hit[0] > time.monotonic():
        return hit[1]
    value = default
    try:
        rows = (sb.table("settings").select("value")
                .eq("company_id", company_id).eq("key", key)
                .execute().data) or []
        if rows and rows[0].get("value") is not None:
            value = json.loads(rows[0]["value"])
    except Exception:
        _log.exception("settings read failed (%s/%s) — using default",
                       company_id, key)
        value = default
    _CACHE[(company_id, key)] = (time.monotonic() + _CACHE_TTL, value)
    return value


def effective_settings(sb, company_id: str) -> dict:
    """{key: {value, default, customized}} for every known key — one query."""
    stored: dict = {}
    try:
        rows = (sb.table("settings").select("key, value")
                .eq("company_id", company_id)
                .in_("key", list(KNOWN_KEYS)).execute().data) or []
        for r in rows:
            try:
                stored[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                _log.warning("broken settings row ignored: %s", r.get("key"))
    except Exception:
        _log.exception("settings bulk read failed — all defaults")
    return {
        k: {"value": stored.get(k, DEFAULTS[k]),
            "default": DEFAULTS[k],
            "customized": k in stored}
        for k in sorted(KNOWN_KEYS)
    }
