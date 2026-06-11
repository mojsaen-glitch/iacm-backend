"""OCC Advanced Delay — POST /occ/flights/{id}/delay.

Covers: role gate, blocked statuses, ETD validation (before STD / before now),
reason-code requirement, successful delay (ETD set, STD untouched, audit), and
crew notification creation.

Run:  py -m pytest tests/test_occ_delay.py -q
"""
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

from app.core.exceptions import ForbiddenError, NotFoundError
from app.api.v1.endpoints.occ import occ_delay_flight


# ── Fake Supabase (records inserts/updates; ignores filter args) ──────────────
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


def _iso(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _flight(**over):
    f = {"id": "f1", "company_id": "c1", "flight_number": "IA-100",
         "origin_code": "BGW", "destination_code": "CAI",
         "departure_time": _iso(1), "arrival_time": _iso(3),
         "status": "scheduled", "aircraft_registration": "YI-ASU"}
    f.update(over)
    return f


def _run(store, data, user=OPS):
    return asyncio.run(occ_delay_flight("f1", data=data, current_user=user, sb=FakeSb(store)))


def _notifs(store):
    out = []
    for b in store.get("notifications_inserts", []):
        out.extend(b if isinstance(b, list) else [b])
    return out


# ── Role gate ─────────────────────────────────────────────────────────────────
def test_ops_role_allowed():
    store = {"flights": [_flight()], "assignments": [], "users": []}
    res = _run(store, {"new_etd": _iso(3), "reason_code": "weather"})
    assert res["ok"] is True


def test_non_ops_role_forbidden():
    store = {"flights": [_flight()]}
    with pytest.raises(ForbiddenError):
        _run(store, {"new_etd": _iso(3), "reason_code": "weather"}, user=CREW_USER)


# ── Status gate ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("st",
    ["cancelled", "departed", "in_air", "landed", "arrived", "completed", "diverted"])
def test_blocked_status_rejected(st):
    store = {"flights": [_flight(status=st)]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"new_etd": _iso(3), "reason_code": "weather"})
    assert ei.value.status_code == 409
    assert "flights_updates" not in store          # nothing persisted


# ── ETD validation ────────────────────────────────────────────────────────────
def test_etd_before_std_rejected():
    store = {"flights": [_flight(departure_time=_iso(4))]}      # STD = now+4h
    with pytest.raises(HTTPException) as ei:
        _run(store, {"new_etd": _iso(2), "reason_code": "weather"})  # ETD < STD
    assert ei.value.status_code == 422


def test_etd_before_now_rejected():
    past_std = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    past_etd = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store = {"flights": [_flight(departure_time=past_std)]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"new_etd": past_etd, "reason_code": "weather"})  # > STD but < now
    assert ei.value.status_code == 422


def test_missing_etd_rejected():
    store = {"flights": [_flight()]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"reason_code": "weather"})
    assert ei.value.status_code == 422


# ── Reason code ───────────────────────────────────────────────────────────────
def test_missing_reason_code_rejected():
    store = {"flights": [_flight()]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"new_etd": _iso(3)})
    assert ei.value.status_code == 422
    assert "flights_updates" not in store


def test_invalid_reason_code_rejected():
    store = {"flights": [_flight()]}
    with pytest.raises(HTTPException) as ei:
        _run(store, {"new_etd": _iso(3), "reason_code": "alien_invasion"})
    assert ei.value.status_code == 422


# ── Not found ─────────────────────────────────────────────────────────────────
def test_flight_not_found():
    store = {"flights": []}
    with pytest.raises(NotFoundError):
        _run(store, {"new_etd": _iso(3), "reason_code": "weather"})


# ── Success: ETD set, STD untouched, metadata + audit ─────────────────────────
def test_successful_delay_sets_etd_keeps_std_and_audits():
    std = _iso(1)
    store = {"flights": [_flight(departure_time=std)], "assignments": [], "users": []}
    res = _run(store, {"new_etd": _iso(3), "reason_code": "technical", "notes": "fog at CAI"})

    assert res["flight"]["status"] == "delayed"
    assert res["impact"]["delay_minutes"] > 0

    upd = store["flights_updates"][0]
    assert upd["status"] == "delayed"
    assert upd["estimated_departure_time"]                 # new ETD persisted
    assert "departure_time" not in upd                     # STD NEVER modified
    assert upd["delay_reason_code"] == "technical"
    assert upd["delay_notes"] == "fog at CAI"
    assert upd["delay_updated_by"] == "u1"
    assert upd["delay_updated_at"]
    assert upd["delay_minutes"] == res["impact"]["delay_minutes"]

    assert any(a["action"] == "occ_delay_flight" for a in store.get("audit_log_inserts", []))


# ── Notifications ─────────────────────────────────────────────────────────────
def test_notifies_crew(monkeypatch):
    import app.api.v1.endpoints.occ as occ_mod
    monkeypatch.setattr(occ_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    store = {
        "flights": [_flight()],
        "assignments": [{"crew_id": "cr1", "duty_type": "operating"},
                        {"crew_id": "cr2", "duty_type": "operating"}],
        "users": [{"id": "usr1", "crew_id": "cr1"}, {"id": "usr2", "crew_id": "cr2"}],
    }
    res = _run(store, {"new_etd": _iso(3), "reason_code": "crew_shortage",
                       "notify_crew": True, "notify_ops": False})
    notifs = _notifs(store)
    assert len(notifs) == 2
    assert all(n["type"] == "flight_disruption" for n in notifs)
    assert res["impact"]["crew_affected"] == 2
    assert res["impact"]["notified"] == 2


def test_no_notifications_when_disabled():
    store = {"flights": [_flight()],
             "assignments": [{"crew_id": "cr1", "duty_type": "operating"}],
             "users": [{"id": "usr1", "crew_id": "cr1"}]}
    _run(store, {"new_etd": _iso(3), "reason_code": "operational",
                 "notify_crew": False, "notify_ops": False})
    assert "notifications_inserts" not in store
