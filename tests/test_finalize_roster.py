"""finalize-roster endpoint — idempotency + under-staffing gate + role gate.

Run:  py -m pytest tests/test_finalize_roster.py -q
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.core.exceptions import ForbiddenError
from app.api.v1.endpoints.flights import (
    finalize_roster, regenerate_gendec, mark_gd_stale_if_finalized,
    _normalize_reg, _validate_reg_format,
)


def _all_notifs(store):
    """Flatten every notification payload inserted into the fake store (inserts
    may be single dicts or lists of dicts)."""
    out = []
    for batch in store.get("notifications_inserts", []):
        out.extend(batch if isinstance(batch, list) else [batch])
    return out


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def insert(self, payload):
        self.store.setdefault(self.name + "_inserts", []).append(payload)
        return self
    def update(self, payload):
        self.store.setdefault(self.name + "_updates", []).append(payload)
        return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


SCHEDULER = {"id": "u1", "role": "scheduler", "company_id": "c1", "is_superuser": False}
CREW_USER = {"id": "u9", "role": "crew", "company_id": "c1", "is_superuser": False}


def _run(store, user=SCHEDULER):
    return asyncio.run(finalize_roster("f1", current_user=user, sb=FakeSb(store), data=None))


def test_non_approver_forbidden():
    store = {"flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737"}]}
    with pytest.raises(ForbiddenError):
        _run(store, user=CREW_USER)


def test_already_finalized_is_idempotent():
    store = {"flights": [{
        "id": "f1", "company_id": "c1", "aircraft_type": "B737",
        "roster_finalized_status": "finalized",
        "roster_finalized_at": "2026-06-01T10:00:00+00:00",
        "roster_finalized_by": "u1",
    }]}
    res = _run(store)
    assert res["already_finalized"] is True
    assert res["crew_notified"] == 0
    # No new notifications were inserted on the idempotent path.
    assert "notifications_inserts" not in store


def test_missing_migration_fails_closed_503():
    # Flight row WITHOUT the roster_finalized_* columns (migration not run).
    store = {"flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737"}],
             "assignments": [{"crew_id": "a"}], "crew": [{"rank": "captain"}]}
    with pytest.raises(HTTPException) as ei:
        _run(store)
    assert ei.value.status_code == 503
    assert "migration is missing" in ei.value.detail
    # FAIL-CLOSED: no notifications sent when state cannot be recorded.
    assert "notifications_inserts" not in store


def test_understaffed_blocks_with_422():
    store = {"flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                          "aircraft_registration": "YI-ASU",
                          "roster_finalized_status": None}],
             "assignments": [], "crew": []}
    with pytest.raises(HTTPException) as ei:
        _run(store)
    assert ei.value.status_code == 422
    assert "Minimum crew requirement not met" in ei.value.detail


def test_complete_roster_finalizes_and_marks_state():
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                     "flight_number": "IA-100", "aircraft_registration": "YI-ASU",
                     "roster_finalized_status": None}],
        "assignments": [{"crew_id": c, "acknowledged": True} for c in ("a", "b", "c", "d", "e")],
        "crew": [{"rank": r} for r in
                 ("captain", "first_officer", "purser", "cabin_crew", "cabin_crew")],
        "users": [{"id": f"usr_{c}", "crew_id": c} for c in ("a", "b", "c", "d", "e")],
    }
    res = _run(store)
    assert res["already_finalized"] is False
    assert res["crew_notified"] == 5
    # Finalisation state persisted + audit written.
    assert store["flights_updates"][0]["roster_finalized_status"] == "finalized"
    # GD becomes ready (downloadable by Flight Ops) on finalise, version bumped.
    assert store["flights_updates"][0]["gd_status"] == "ready"
    assert store["flights_updates"][0]["gd_version"] == 1
    assert res["gd_status"] == "ready"
    assert any(i["action"] == "finalize_roster" for i in store.get("audit_log_inserts", []))


def test_refinalize_allowed_when_stale():
    # A finalised flight whose crew changed (gd_status='stale') with a complete
    # roster can be re-finalised (NOT treated as idempotent), refreshing the GD.
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                     "flight_number": "IA-1", "aircraft_registration": "YI-ASU",
                     "roster_finalized_status": "finalized", "gd_status": "stale",
                     "gd_version": 3}],
        "assignments": [{"crew_id": c, "acknowledged": True} for c in ("a", "b", "c", "d", "e")],
        "crew": [{"rank": r} for r in
                 ("captain", "first_officer", "purser", "cabin_crew", "cabin_crew")],
        "users": [{"id": f"usr_{c}", "crew_id": c} for c in ("a", "b", "c", "d", "e")],
    }
    res = _run(store)
    assert res["already_finalized"] is False
    assert res["gd_status"] == "ready"
    assert store["flights_updates"][0]["gd_version"] == 4


def test_mark_gd_stale_flips_ready_to_stale_and_notifies():
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-1",
                     "origin_code": "BGW", "destination_code": "CAI",
                     "roster_finalized_status": "finalized", "gd_status": "ready"}],
        "users": [{"id": "o1", "role": "flight_operations"}],
    }
    mark_gd_stale_if_finalized(FakeSb(store), "c1", "f1", actor={"id": "u1"})
    assert store["flights_updates"][0]["gd_status"] == "stale"
    assert any(n["type"] == "gd_stale" for n in _all_notifs(store))
    assert any(i["action"] == "gd_marked_stale" for i in store.get("audit_log_inserts", []))


def test_mark_gd_stale_noop_when_not_finalized():
    store = {"flights": [{"id": "f1", "company_id": "c1",
                          "roster_finalized_status": None, "gd_status": None}]}
    mark_gd_stale_if_finalized(FakeSb(store), "c1", "f1")
    assert "flights_updates" not in store  # nothing touched


def test_mark_gd_stale_idempotent_when_already_stale():
    store = {"flights": [{"id": "f1", "company_id": "c1",
                          "roster_finalized_status": "finalized", "gd_status": "stale"}]}
    mark_gd_stale_if_finalized(FakeSb(store), "c1", "f1")
    assert "flights_updates" not in store  # already stale → no re-notify


def _run_regen(store, user=SCHEDULER):
    return asyncio.run(regenerate_gendec("f1", current_user=user, sb=FakeSb(store)))


def test_regenerate_requires_finalized_409():
    store = {"flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                          "aircraft_registration": "YI-ASU", "gd_status": None,
                          "roster_finalized_status": None}],
             "assignments": [], "crew": []}
    with pytest.raises(HTTPException) as ei:
        _run_regen(store)
    assert ei.value.status_code == 409


def test_regenerate_sets_ready_and_bumps_version():
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                     "flight_number": "IA-1", "aircraft_registration": "YI-ASU",
                     "gd_status": "stale", "gd_version": 2,
                     "roster_finalized_status": "finalized"}],
        "assignments": [{"crew_id": c, "acknowledged": True} for c in ("a", "b", "c", "d", "e")],
        "crew": [{"rank": r} for r in
                 ("captain", "first_officer", "purser", "cabin_crew", "cabin_crew")],
        "users": [{"id": "o1", "role": "ops_manager"}],
    }
    res = _run_regen(store)
    assert res["gd_status"] == "ready"
    assert res["gd_version"] == 3
    assert any(n["type"] == "gd_ready" for n in _all_notifs(store))


def test_missing_reg_blocks_finalize_with_422():
    # Migrated flight with a COMPLETE crew but NO aircraft_registration: the REG
    # gate fires before the (passing) min-crew gate, so 422 mentions REG.
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                     "roster_finalized_status": None}],
        "assignments": [{"crew_id": c, "acknowledged": True} for c in ("a", "b", "c", "d", "e")],
        "crew": [{"rank": r} for r in
                 ("captain", "first_officer", "purser", "cabin_crew", "cabin_crew")],
    }
    with pytest.raises(HTTPException) as ei:
        _run(store)
    assert ei.value.status_code == 422
    assert "REG" in ei.value.detail
    # No finalisation persisted / no crew notified when REG is missing.
    assert "flights_updates" not in store
    assert "notifications_inserts" not in store


def test_reg_normalize_and_format():
    assert _normalize_reg("  yi-asu ") == "YI-ASU"
    assert _normalize_reg(None) == ""
    # Valid tails (Iraqi + a foreign-style) pass; junk raises 422.
    _validate_reg_format("YI-ASU")
    _validate_reg_format("YI-AQY")
    with pytest.raises(HTTPException):
        _validate_reg_format("A320")          # that's an aircraft TYPE, not a tail
    with pytest.raises(HTTPException):
        _validate_reg_format("THIS IS NOT A TAIL")
