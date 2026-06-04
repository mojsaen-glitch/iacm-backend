"""finalize-roster endpoint — idempotency + under-staffing gate + role gate.

Run:  py -m pytest tests/test_finalize_roster.py -q
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.core.exceptions import ForbiddenError
from app.api.v1.endpoints.flights import finalize_roster


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
                          "roster_finalized_status": None}],
             "assignments": [], "crew": []}
    with pytest.raises(HTTPException) as ei:
        _run(store)
    assert ei.value.status_code == 422
    assert "Minimum crew requirement not met" in ei.value.detail


def test_complete_roster_finalizes_and_marks_state():
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                     "flight_number": "IA-100", "roster_finalized_status": None}],
        "assignments": [{"crew_id": c} for c in ("a", "b", "c", "d", "e")],
        "crew": [{"rank": r} for r in
                 ("captain", "first_officer", "purser", "cabin_crew", "cabin_crew")],
        "users": [{"id": f"usr_{c}", "crew_id": c} for c in ("a", "b", "c", "d", "e")],
    }
    res = _run(store)
    assert res["already_finalized"] is False
    assert res["crew_notified"] == 5
    # Finalisation state persisted + audit written.
    assert store["flights_updates"][0]["roster_finalized_status"] == "finalized"
    assert any(i["action"] == "finalize_roster" for i in store.get("audit_log_inserts", []))
