"""Company operational settings — batch 1 (loader + endpoints, NO gate wiring).

Pins: defaults == today's constants (parity by construction/by test), no-row ⇒
no behaviour change, per-company isolation, full audit, strict validation,
admin-only access.

Run:  py -m pytest tests/test_operational_settings.py -q
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.operational_settings import (
    get_operational_settings, put_operational_setting,
)
from app.core.company_settings import (
    DEFAULTS, KNOWN_KEYS, get_company_setting, invalidate_settings_cache,
)
from app.core.exceptions import ForbiddenError


class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): return self
    def eq(self, f, v):  self._filters.append(("eq", f, v));  return self
    def in_(self, f, v): self._filters.append(("in", f, set(v))); return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self

    def _match(self, row):
        for op, f, v in self._filters:
            if op == "eq" and row.get(f) != v: return False
            if op == "in" and row.get(f) not in v: return False
        return True

    def execute(self):
        rows = self.sb.store.get(self.table, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            self.sb.store[self.table] = rows
            self.sb.ops.append(("insert", self.table, items))
            return _R(items)
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            self.sb.ops.append(("update", self.table, self._payload))
            return _R([dict(r) for r in hit])
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store=None):
        self.store = {k: [dict(r) for r in v] for k, v in (store or {}).items()}
        self.ops = []

    def table(self, name): return _Q(self, name)

    def audits(self, action):
        return [i for op, t, items in self.ops
                if op == "insert" and t == "audit_log"
                for i in items if i.get("action") == action]


ADMIN_A = {"id": "uA", "role": "admin", "company_id": "cA",
           "name_ar": "إدارة A", "is_superuser": False}
ADMIN_B = {"id": "uB", "role": "admin", "company_id": "cB",
           "name_ar": "إدارة B", "is_superuser": False}
SCHED = {"id": "uS", "role": "scheduler", "company_id": "cA",
         "is_superuser": False}


@pytest.fixture(autouse=True)
def _fresh_cache():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


def _get(sb, user=ADMIN_A):
    return asyncio.run(get_operational_settings(current_user=user, sb=sb))


def _put(sb, key, value, user=ADMIN_A):
    return asyncio.run(put_operational_setting(key, {"value": value},
                                               current_user=user, sb=sb))


# ── 1) GET returns every known key with TODAY's defaults ─────────────────────
def test_get_returns_all_known_keys_with_current_defaults():
    res = _get(FakeSb())
    s = res["settings"]
    assert set(s) == set(KNOWN_KEYS)
    assert all(not v["customized"] for v in s.values())
    # Spot-pin literal values straight from the plan/current code:
    assert s["ops.fleet.complement"]["value"]["A320"] == {
        "min_pilots": 2, "max_pilots": 2, "min_cabin": 3,
        "max_cabin": 4, "engineers": 0}
    assert s["ops.fleet.complement"]["value"]["B737"]["max_cabin"] == 5
    assert s["ops.ui.low_hours_threshold"]["value"] == 40
    assert s["ftl.min_rest_international_hours"]["value"] == 12.0
    assert s["ops.boarding_lead_minutes"]["value"] == 30


def test_defaults_match_existing_constants_parity():
    """Parity pins — these fail if either side drifts."""
    from app.core.fleet_complement import _FLEET, _GENERIC
    from app.api.v1.endpoints import occ as occ_mod
    from app.core.compliance_engine import (
        IRAQI_AIRPORTS, MAX_28DAY_HOURS, MIN_REST_DOMESTIC,
        MIN_REST_INTERNATIONAL,
    )
    from app.core.config import settings as cfg

    comp = DEFAULTS["ops.fleet.complement"]
    for t, (mp, xp, mc, xc, eng) in _FLEET.items():
        assert comp[t] == {"min_pilots": mp, "max_pilots": xp,
                           "min_cabin": mc, "max_cabin": xc, "engineers": eng}
    assert comp["_generic"]["min_cabin"] == _GENERIC[2]
    assert set(DEFAULTS["ops.delay.reason_codes"]) == occ_mod._DELAY_REASON_CODES
    assert set(DEFAULTS["ops.aircraft_change.reason_codes"]) \
        == occ_mod._AIRCRAFT_CHANGE_REASONS
    assert set(DEFAULTS["ops.airports.domestic"]) == IRAQI_AIRPORTS
    assert DEFAULTS["ftl.max_monthly_hours"] == cfg.MAX_MONTHLY_HOURS
    assert DEFAULTS["ftl.max_yearly_hours"] == cfg.MAX_YEARLY_HOURS
    assert DEFAULTS["ftl.max_28day_hours"] == MAX_28DAY_HOURS
    assert DEFAULTS["ftl.min_rest_domestic_hours"] == MIN_REST_DOMESTIC
    assert DEFAULTS["ftl.min_rest_international_hours"] == MIN_REST_INTERNATIONAL


# ── 2) No row ⇒ loader returns the default (no behaviour change) ─────────────
def test_loader_falls_open_to_default():
    sb = FakeSb()
    assert get_company_setting(sb, "cA", "ops.ui.low_hours_threshold") == 40
    # Broken stored JSON must ALSO fall open, never raise:
    sb2 = FakeSb({"settings": [{"company_id": "cA",
                                "key": "ops.ui.low_hours_threshold",
                                "value": "{not json"}]})
    assert get_company_setting(sb2, "cA", "ops.ui.low_hours_threshold") == 40


# ── 3) Per-company isolation ─────────────────────────────────────────────────
def test_company_isolation():
    sb = FakeSb()
    _put(sb, "ops.ui.low_hours_threshold", 55, user=ADMIN_A)
    invalidate_settings_cache()
    assert get_company_setting(sb, "cA", "ops.ui.low_hours_threshold") == 55
    assert get_company_setting(sb, "cB", "ops.ui.low_hours_threshold") == 40
    res_b = _get(sb, user=ADMIN_B)
    assert res_b["settings"]["ops.ui.low_hours_threshold"]["customized"] is False


# ── 4) PUT writes a full before/after audit ──────────────────────────────────
def test_put_audits_before_after():
    sb = FakeSb()
    _put(sb, "ops.boarding_lead_minutes", 45)
    a = sb.audits("operational_setting_updated")
    assert len(a) == 1 and a[0]["company_id"] == "cA"
    before = json.loads(a[0]["before_data"]); after = json.loads(a[0]["after_data"])
    assert before["was_default"] is True and before["stored_value"] is None
    assert after["value"] == 45
    # Second update: before now carries the stored value.
    _put(sb, "ops.boarding_lead_minutes", 20)
    before2 = json.loads(sb.audits("operational_setting_updated")[1]["before_data"])
    assert before2["stored_value"] == 45 and before2["was_default"] is False


# ── 5) Bad values rejected with 422 (and nothing written) ────────────────────
@pytest.mark.parametrize("key,value,fragment", [
    ("no.such.key", 1, "غير معروف"),
    ("ops.ui.low_hours_threshold", "forty", "رقم"),
    ("ops.ui.low_hours_threshold", -5, "أقل"),
    ("ops.delay.reason_codes", [], "غير فارغة"),
    ("ops.delay.reason_codes", ["a", "a"], "مكررة"),
    ("ops.fleet.complement",
     {"A320": {"min_pilots": 3, "max_pilots": 2, "min_cabin": 3,
               "max_cabin": 4, "engineers": 0}}, "min_pilots"),
    ("ops.fleet.complement", {"A320": {"min_pilots": 2}}, "الحقول"),
    ("ops.acceptance_reminders", {"gentle_hours": 48}, "الحقول"),
])
def test_validation_rejects(key, value, fragment):
    sb = FakeSb()
    with pytest.raises(HTTPException) as e:
        _put(sb, key, value)
    assert e.value.status_code == 422
    assert fragment in str(e.value.detail)
    assert sb.store.get("settings", []) == []        # nothing persisted
    assert not sb.audits("operational_setting_updated")


def test_missing_value_field_rejected():
    with pytest.raises(HTTPException) as e:
        asyncio.run(put_operational_setting("ops.boarding_lead_minutes", {},
                                            current_user=ADMIN_A, sb=FakeSb()))
    assert e.value.status_code == 422


# ── 6) Role gate ─────────────────────────────────────────────────────────────
def test_non_admin_forbidden():
    with pytest.raises(ForbiddenError):
        _get(FakeSb(), user=SCHED)
    with pytest.raises(ForbiddenError):
        _put(FakeSb(), "ops.boarding_lead_minutes", 30, user=SCHED)


# ── Cache: PUT invalidates; reads are cached within TTL ─────────────────────
def test_put_invalidates_cache():
    sb = FakeSb()
    assert get_company_setting(sb, "cA", "ops.ui.low_hours_threshold") == 40
    _put(sb, "ops.ui.low_hours_threshold", 60)
    assert get_company_setting(sb, "cA", "ops.ui.low_hours_threshold") == 60
