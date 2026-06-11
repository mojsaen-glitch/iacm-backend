"""OCC Phase 3C — Change Aircraft (POST /occ/flights/{id}/change-aircraft).

Covers: role gate, blocked statuses, REG validation, no-op rejection, reason
requirement, successful change (REG/type updated + audit), GD-stale marking,
crew type-rating impact, and crew notification.

Run:  py -m pytest tests/test_occ_change_aircraft.py -q
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.core.exceptions import ForbiddenError, NotFoundError
from app.api.v1.endpoints.occ import occ_change_aircraft, _crew_qualified


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


OPS       = {"id": "u1", "role": "ops_manager", "company_id": "c1", "is_superuser": False}
CREW_USER = {"id": "u9", "role": "crew",        "company_id": "c1", "is_superuser": False}


def _flight(**over):
    f = {"id": "f1", "company_id": "c1", "flight_number": "IA-100",
         "origin_code": "BGW", "destination_code": "CAI", "status": "scheduled",
         "aircraft_registration": "YI-ASU", "aircraft_type": "A320"}
    f.update(over)
    return f


def _run(store, data, user=OPS):
    return asyncio.run(occ_change_aircraft("f1", data=data, current_user=user, sb=FakeSb(store)))


def _notifs(store):
    out = []
    for b in store.get("notifications_inserts", []):
        out.extend(b if isinstance(b, list) else [b])
    return out


# ── Role gate ─────────────────────────────────────────────────────────────────
def test_ops_allowed():
    store = {"flights": [_flight()], "assignments": [], "users": []}
    res = _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "maintenance"})
    assert res["ok"] is True
    assert res["flight"]["aircraft_registration"] == "YI-AQY"


def test_crew_user_forbidden():
    store = {"flights": [_flight()]}
    with pytest.raises(ForbiddenError):
        _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "maintenance"}, user=CREW_USER)


# ── Status gate ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("st",
    ["cancelled", "departed", "in_air", "landed", "arrived", "completed", "diverted"])
def test_blocked_status(st):
    store = {"flights": [_flight(status=st)]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "maintenance"})
    assert ei.value.status_code == 409
    assert "flights_updates" not in store


# ── REG validation / no-op ────────────────────────────────────────────────────
def test_missing_reg():
    store = {"flights": [_flight()]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"reason_code": "maintenance"})
    assert ei.value.status_code == 422


def test_invalid_reg_format():
    store = {"flights": [_flight()]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"aircraft_registration": "A320", "reason_code": "maintenance"})
    assert ei.value.status_code == 422


def test_no_change_rejected():
    # Same REG and same type → nothing to do.
    store = {"flights": [_flight(aircraft_registration="YI-ASU", aircraft_type="A320")]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"aircraft_registration": "YI-ASU", "aircraft_type": "A320",
                     "reason_code": "swap"})
    assert ei.value.status_code == 422
    assert "flights_updates" not in store


# ── Reason code ───────────────────────────────────────────────────────────────
def test_missing_reason():
    store = {"flights": [_flight()]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"aircraft_registration": "YI-AQY"})
    assert ei.value.status_code == 422


def test_invalid_reason():
    store = {"flights": [_flight()]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "banana"})
    assert ei.value.status_code == 422


# ── Not found ─────────────────────────────────────────────────────────────────
def test_not_found():
    store = {"flights": []}
    with pytest.raises(NotFoundError):
        _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "maintenance"})


# ── Success: REG/type persisted + audit ───────────────────────────────────────
def test_success_updates_and_audits():
    store = {"flights": [_flight()], "assignments": [], "users": []}
    res = _run(store, {"aircraft_registration": "yi-aqy", "aircraft_type": "B737",
                       "reason_code": "aog", "notes": "tech swap"})
    upd = store["flights_updates"][0]
    assert upd["aircraft_registration"] == "YI-AQY"     # normalised upper
    assert upd["aircraft_type"] == "B737"
    assert res["impact"]["type_changed"] is True
    audit = [a for a in store.get("audit_log_inserts", []) if a["action"] == "occ_change_aircraft"]
    assert audit and '"to_reg": "YI-AQY"' in audit[0]["after_data"]


def test_reg_only_change_keeps_type():
    store = {"flights": [_flight(aircraft_type="A320")], "assignments": [], "users": []}
    res = _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "swap"})
    assert store["flights_updates"][0]["aircraft_type"] == "A320"   # unchanged
    assert res["impact"]["type_changed"] is False
    assert res["impact"]["crew_unqualified"] == 0


# ── GD stale on REG change ────────────────────────────────────────────────────
def test_finalized_gd_marked_stale():
    store = {"flights": [_flight(roster_finalized_status="finalized", gd_status="ready")],
             "assignments": [], "users": []}
    res = _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "maintenance"})
    assert res["impact"]["gd_marked_stale"] is True
    # mark_gd_stale_if_finalized flips gd_status to 'stale'.
    assert any(u.get("gd_status") == "stale" for u in store.get("flights_updates", []))


# ── Crew type-rating impact ───────────────────────────────────────────────────
def test_crew_unqualified_counted_on_type_change():
    store = {
        "flights": [_flight(aircraft_type="A320")],
        "assignments": [{"crew_id": "cr1", "duty_type": "operating"},
                        {"crew_id": "cr2", "duty_type": "operating"}],
        "crew": [
            {"id": "cr1", "aircraft_qualifications": ["B737"], "fleet": "B737"},  # qualified
            {"id": "cr2", "aircraft_qualifications": ["A320"], "fleet": "A320"},  # NOT for B737
        ],
        "users": [],
    }
    res = _run(store, {"aircraft_registration": "YI-AQY", "aircraft_type": "B737",
                       "reason_code": "capacity", "notify_crew": False})
    assert res["impact"]["crew_total"] == 2
    assert res["impact"]["crew_unqualified"] == 1


# ── Notifications ─────────────────────────────────────────────────────────────
def test_notifies_crew(monkeypatch):
    import app.api.v1.endpoints.occ as occ_mod
    monkeypatch.setattr(occ_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    store = {
        "flights": [_flight()],
        "assignments": [{"crew_id": "cr1", "duty_type": "operating"}],
        "users": [{"id": "usr1", "crew_id": "cr1"}],
    }
    res = _run(store, {"aircraft_registration": "YI-AQY", "reason_code": "swap",
                       "notify_crew": True, "notify_ops": False})
    notifs = _notifs(store)
    assert len(notifs) == 1
    assert notifs[0]["type"] == "flight_disruption"
    assert res["impact"]["notified"] == 1


# ── Crew Revalidation: un-rated crew → schedulers notified + audited ──────────
def test_unqualified_crew_notifies_schedulers(monkeypatch):
    import app.api.v1.endpoints.occ as occ_mod
    monkeypatch.setattr(occ_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    store = {
        "flights": [_flight(aircraft_type="A320")],
        "assignments": [{"crew_id": "cr1", "duty_type": "operating"}],
        "crew": [{"id": "cr1", "aircraft_qualifications": ["A320"], "fleet": "A320"}],  # not B737
        "users": [{"id": "sch1", "role": "scheduler"},
                  {"id": "mv1", "role": "flight_movement"}],
    }
    res = _run(store, {"aircraft_registration": "YI-AQY", "aircraft_type": "B737",
                       "reason_code": "capacity", "notify_crew": False, "notify_ops": False})
    assert res["impact"]["crew_review_required"] is True
    assert res["impact"]["scheduler_notified"] == 2          # both scheduler-role users
    notifs = _notifs(store)
    assert notifs and all(n["type"] == "crew_review_required" for n in notifs)
    audit = [a for a in store.get("audit_log_inserts", []) if a["action"] == "occ_change_aircraft"][0]
    assert '"crew_revalidation_required": true' in audit["after_data"]
    assert '"scheduler_notified": true' in audit["after_data"]


def test_qualified_crew_no_revalidation():
    store = {
        "flights": [_flight(aircraft_type="A320")],
        "assignments": [{"crew_id": "cr1", "duty_type": "operating"}],
        "crew": [{"id": "cr1", "aircraft_qualifications": ["A320", "B737"], "fleet": "B737"}],
        "users": [],
    }
    res = _run(store, {"aircraft_registration": "YI-AQY", "aircraft_type": "B737",
                       "reason_code": "swap", "notify_crew": False})
    assert res["impact"]["crew_review_required"] is False
    assert res["impact"]["scheduler_notified"] == 0


# ── Tolerant qualification helper ─────────────────────────────────────────────
def test_qualified_helper_tolerant():
    assert _crew_qualified({"aircraft_qualifications": ["B738"]}, "738") is True
    assert _crew_qualified({"fleet": "A320"}, "A320") is True
    assert _crew_qualified({"aircraft_qualifications": ["A320"]}, "B737") is False
    assert _crew_qualified({}, "") is True          # no type → no constraint
