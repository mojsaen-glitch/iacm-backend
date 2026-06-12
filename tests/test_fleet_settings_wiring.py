"""Batch 2 — fleet templates wired to per-company settings (ONLY the three
ops.fleet.* keys). Pins: default equivalence, isolation, the publish/finalize/
GD gates honour a customization, caps honour custom max, operational stays
advisory.

Run:  py -m pytest tests/test_fleet_settings_wiring.py -q
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app.core.fleet_complement import (
    min_required_for_category, operational_expected_by_role,
    required_for_category,
)
from app.api.v1.endpoints.flights import gd_clearance, publish_flight


class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def eq(self, f, v):  self._filters.append(("eq", f, v));  return self
    def neq(self, f, v): self._filters.append(("neq", f, v)); return self
    def in_(self, f, v): self._filters.append(("in", f, set(v))); return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self

    def _match(self, row):
        for op, f, v in self._filters:
            if op == "eq" and row.get(f) != v: return False
            if op == "neq" and row.get(f) == v: return False
            if op == "in" and row.get(f) not in v: return False
        return True

    def execute(self):
        rows = self.sb.store.get(self.table, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            self.sb.store[self.table] = rows
            return _R(items)
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            return _R([dict(r) for r in hit])
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store=None):
        self.store = {k: [dict(r) for r in v] for k, v in (store or {}).items()}

    def table(self, name): return _Q(self, name)


ADMIN = {"id": "u1", "role": "admin", "company_id": "c1",
         "name_ar": "الإدارة", "is_superuser": False}

A320_CUSTOM_MIN4 = {
    "A320": {"min_pilots": 2, "max_pilots": 2, "min_cabin": 4,
             "max_cabin": 4, "engineers": 0},
}


def _setting_row(cid, key, value):
    return {"company_id": cid, "key": key, "value": json.dumps(value)}


# ── 2) Explicit default equivalence (no settings rows) ───────────────────────
def test_default_equivalence_a320_b737_generic():
    sb = FakeSb()  # empty settings table — must equal the constants exactly
    for cid in ("cX",):
        assert min_required_for_category("A320", "cabin", sb=sb, company_id=cid) == 3
        assert min_required_for_category("A320", "pilot", sb=sb, company_id=cid) == 2
        assert min_required_for_category("B737", "cabin", sb=sb, company_id=cid) == 3
        assert min_required_for_category("UNKNOWN", "cabin", sb=sb, company_id=cid) == 3
        assert required_for_category("A320", "cabin", 2.0, sb=sb, company_id=cid) == 4
        assert required_for_category("B737", "cabin", 2.0, sb=sb, company_id=cid) == 5
        assert required_for_category("B777", "pilot", 9.0, sb=sb, company_id=cid) == 4
        assert required_for_category("B777", "pilot", 5.0, sb=sb, company_id=cid) == 2
    # And entirely without context (pure call) — identical:
    assert min_required_for_category("A320", "cabin") == 3


# ── 3) Customizing company A never touches company B ─────────────────────────
def test_company_isolation():
    sb = FakeSb({"settings": [
        _setting_row("cA", "ops.fleet.complement", A320_CUSTOM_MIN4)]})
    assert min_required_for_category("A320", "cabin", sb=sb, company_id="cA") == 4
    assert min_required_for_category("A320", "cabin", sb=sb, company_id="cB") == 3


# ── 5) Assignment cap honours a customized max ────────────────────────────────
def test_cap_honours_custom_max():
    custom = {"A320": {"min_pilots": 2, "max_pilots": 2, "min_cabin": 3,
                       "max_cabin": 6, "engineers": 0}}
    sb = FakeSb({"settings": [_setting_row("cA", "ops.fleet.complement", custom)]})
    assert required_for_category("A320", "cabin", 2.0, sb=sb, company_id="cA") == 6
    assert required_for_category("A320", "cabin", 2.0, sb=sb, company_id="cB") == 4


# ── Broken stored row fails OPEN to the constants ─────────────────────────────
def test_broken_row_falls_back():
    sb = FakeSb({"settings": [
        {"company_id": "cA", "key": "ops.fleet.complement", "value": "{broken"}]})
    assert min_required_for_category("A320", "cabin", sb=sb, company_id="cA") == 3


# ── 4 + 6) The publish / GD gates honour the customization ───────────────────
def _flight_store(cid="c1", with_custom_min4=False, finalized=False):
    """A320 with capt + F/O + 3 CC operating — meets TODAY's floor exactly."""
    members = [("cap", "captain"), ("fo", "first_officer"),
               ("cc1", "cabin_crew"), ("cc2", "cabin_crew"), ("cc3", "cabin_crew")]
    store = {
        "flights": [{"id": "f1", "company_id": cid, "flight_number": "IA-2",
                     "origin_code": "BGW", "destination_code": "EBL",
                     "aircraft_type": "A320", "aircraft_registration": "YI-ASA",
                     "publish_status": "published" if finalized else "draft",
                     "status": "scheduled",
                     "roster_finalized_status": "finalized" if finalized else "",
                     "gd_status": "ready" if finalized else "", "gd_version": 1}],
        "crew": [{"id": c, "company_id": cid, "rank": r, "status": "active",
                  "full_name_ar": f"عضو {c}", "passport_number": f"P-{c}"}
                 for c, r in members],
        "assignments": [{"id": f"a{i}", "flight_id": "f1", "crew_id": c,
                         "duty_type": "operating", "acknowledged": True,
                         "declined": False, "admin_confirmed": False}
                        for i, (c, _r) in enumerate(members)],
        "users": [], "notifications": [], "audit_log": [],
        "settings": ([_setting_row(cid, "ops.fleet.complement",
                                   A320_CUSTOM_MIN4)]
                     if with_custom_min4 else []),
    }
    return store


def test_publish_blocked_when_company_raises_cabin_floor(monkeypatch):
    import app.api.v1.endpoints.flights as fl_mod
    from app.services import push_service
    monkeypatch.setattr(push_service, "send_to_users", lambda *a, **k: 0)
    monkeypatch.setattr(fl_mod, "_insert_role_notifications",
                        lambda *a, **k: 0)

    # Default floor (3 CC) → publishes fine.
    sb_ok = FakeSb(_flight_store(with_custom_min4=False))
    asyncio.run(publish_flight("f1", current_user=ADMIN, sb=sb_ok))
    assert sb_ok.store["flights"][0]["publish_status"] == "published"

    # Company floor raised to 4 → the SAME roster is now short → 422.
    # (drop the 60s cache entry the first half just created for c1)
    from app.core.company_settings import invalidate_settings_cache
    invalidate_settings_cache("c1")
    sb_no = FakeSb(_flight_store(with_custom_min4=True))
    with pytest.raises(HTTPException) as e:
        asyncio.run(publish_flight("f1", current_user=ADMIN, sb=sb_no))
    assert e.value.status_code == 422
    assert "المقصورة" in str(e.value.detail)           # cabin 3/4 named
    assert sb_no.store["flights"][0]["publish_status"] == "draft"


def test_gd_clearance_uses_same_customized_shortfall():
    # Fully cleared flight under TODAY's floor…
    ok = asyncio.run(gd_clearance("f1", current_user=ADMIN,
                                  sb=FakeSb(_flight_store(finalized=True))))
    assert ok["allowed"] is True
    # …blocked once the company raises the floor (finalize/GD share the rule).
    from app.core.company_settings import invalidate_settings_cache
    invalidate_settings_cache("c1")
    res = asyncio.run(gd_clearance(
        "f1", current_user=ADMIN,
        sb=FakeSb(_flight_store(finalized=True, with_custom_min4=True))))
    assert res["allowed"] is False
    assert any("المقصورة" in r for r in res["reasons"])


# ── 7) Operational complement stays ADVISORY ─────────────────────────────────
def test_operational_customization_never_blocks_publish(monkeypatch):
    from app.services import push_service
    import app.api.v1.endpoints.flights as fl_mod
    monkeypatch.setattr(push_service, "send_to_users", lambda *a, **k: 0)
    monkeypatch.setattr(fl_mod, "_insert_role_notifications",
                        lambda *a, **k: 0)
    store = _flight_store()
    store["settings"] = [_setting_row(
        "c1", "ops.fleet.operational_complement",
        {"A320": {"ame": 2, "lsh": 1, "ifso": 5}})]
    sb = FakeSb(store)
    # No AME/IFSO assigned at all — publish still goes through (advisory only).
    asyncio.run(publish_flight("f1", current_user=ADMIN, sb=sb))
    assert sb.store["flights"][0]["publish_status"] == "published"
    # And the customized expectation is readable through the resolver:
    exp = operational_expected_by_role("A320", sb=sb, company_id="c1")
    assert exp["in_flight_security_officer"] == 5
    assert exp["aircraft_maintenance_engineer"] == 2
