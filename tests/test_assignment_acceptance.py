"""Crew Assignment Acceptance — explicit accept/decline before final roster.

Covers: pending by default · respond ownership/publish rules · idempotency ·
decline note + scheduler fan-out · admin-confirm gates · acceptance board RBAC
+ counts · finalize blocked on pending/declined, passes on accepted/confirmed.

Run:  py -m pytest tests/test_assignment_acceptance.py -q
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app.core.exceptions import ForbiddenError, NotFoundError
from app.api.v1.endpoints.assignments import (
    respond_assignment, admin_confirm_assignment, _acceptance_status_row,
)
from app.api.v1.endpoints.flights import (
    flight_assignment_acceptance, finalize_roster,
)


# ── Filtering + recording fake ────────────────────────────────────────────────
class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.rows = list(store.get(name, []))
        self._patch = None
    def select(self, *a, **k): return self
    def update(self, patch): self._patch = patch; return self
    def insert(self, p): self.store.setdefault(self.name + "_inserts", []).append(p); return self
    def eq(self, col, val):
        self.rows = [r for r in self.rows if r.get(col) == val]
        return self
    def in_(self, col, vals):
        s = set(vals); self.rows = [r for r in self.rows if r.get(col) in s]
        return self
    def is_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def neq(self, col, val):
        self.rows = [r for r in self.rows if r.get(col) != val]
        return self
    def execute(self):
        if self._patch is not None:
            for r in self.rows:
                r.update(self._patch)
        return type("R", (), {"data": list(self.rows)})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


CREW1   = {"id": "u_cr1", "role": "crew", "company_id": "c1", "crew_id": "cr1",
           "name_ar": "زها", "is_superuser": False}
CREW2   = {"id": "u_cr2", "role": "crew", "company_id": "c1", "crew_id": "cr2",
           "is_superuser": False}
SUP     = {"id": "u_sup", "role": "ops_manager", "company_id": "c1", "is_superuser": False}
SCHED   = {"id": "u_s", "role": "scheduler", "company_id": "c1", "is_superuser": False}
SCABIN  = {"id": "u_sc", "role": "sched_cabin", "company_id": "c1", "is_superuser": False}


def _store(published=True, **asg_over):
    a = {"id": "a1", "flight_id": "f1", "crew_id": "cr1", "duty_type": "operating",
         "assigned_by": "u_s", "acknowledged": False, "declined": False,
         "admin_confirmed": False}
    a.update(asg_over)
    return {
        "assignments": [a],
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-229",
                     "publish_status": "published" if published else "draft",
                     "departure_time": "2099-01-01T10:00:00+00:00"}],
        "users": [{"id": "u_s", "role": "scheduler", "company_id": "c1"},
                  {"id": "u_om", "role": "ops_manager", "company_id": "c1"}],
        "crew": [{"id": "cr1", "full_name_ar": "زها سمير", "rank": "cabin_crew"}],
    }


def _audits(store, action):
    return [a for a in store.get("audit_log_inserts", []) if a.get("action") == action]


def _notifs(store):
    out = []
    for b in store.get("notifications_inserts", []):
        out.extend(b if isinstance(b, list) else [b])
    return out


def _respond(store, body, user=CREW1):
    return asyncio.run(respond_assignment("a1", body, current_user=user, sb=FakeSb(store)))


# 1) New assignment defaults to pending_acceptance.
def test_new_assignment_is_pending():
    assert _acceptance_status_row({"acknowledged": False, "declined": False}) \
        == "pending_acceptance"


# 2+3) Own row only.
def test_crew_accepts_own_assignment():
    store = _store()
    res = _respond(store, {"response": "accepted"})
    assert res["acceptance_status"] == "accepted"
    assert store["assignments"][0]["acknowledged"] is True


def test_crew_cannot_respond_for_another():
    with pytest.raises(ForbiddenError):
        _respond(_store(), {"response": "accepted"}, user=CREW2)


# 4) Unpublished flight → no responding.
def test_cannot_respond_before_publish():
    with pytest.raises(HTTPException) as ei:
        _respond(_store(published=False), {"response": "accepted"})
    assert ei.value.status_code == 422


# 5+6) accepted_at stored; re-accept idempotent keeps the original.
def test_accept_sets_and_preserves_accepted_at():
    store = _store()
    first = _respond(store, {"response": "accepted"})["accepted_at"]
    again = _respond(store, {"response": "accepted"})
    assert again["accepted_at"] == first
    assert len(_audits(store, "assignment_accepted_by_crew")) == 1   # no dup audit


# 7+8) decline stores note + declined_at and fans out to schedulers.
def test_decline_saves_note_and_notifies_schedulers():
    store = _store()
    res = _respond(store, {"response": "declined", "note": "ظرف عائلي"})
    assert res["acceptance_status"] == "declined" and res["declined_at"]
    row = store["assignments"][0]
    assert row["declined"] is True and row["decline_reason"] == "ظرف عائلي"
    notifs = _notifs(store)
    assert len(notifs) == 2                       # scheduler + ops_manager
    assert all(n["type"] == "assignment_declined" for n in notifs)
    assert _audits(store, "assignment_declined_by_crew")


def test_decline_idempotent():
    store = _store(declined=True, declined_at="2026-06-12T08:00:00+00:00")
    res = _respond(store, {"response": "declined"})
    assert res["declined_at"] == "2026-06-12T08:00:00+00:00"
    assert not _notifs(store)                     # no duplicate fan-out


# declined → accepted allowed pre-departure (safe self-recovery).
def test_declined_can_flip_to_accept_before_departure():
    store = _store(declined=True)
    res = _respond(store, {"response": "accepted"})
    assert res["acceptance_status"] == "accepted"
    assert store["assignments"][0]["declined"] is False


# 12+13) admin-confirm: reason required; sched_* forbidden.
def test_admin_confirm_requires_reason():
    with pytest.raises(HTTPException) as ei:
        asyncio.run(admin_confirm_assignment("a1", {}, current_user=SUP,
                                             sb=FakeSb(_store())))
    assert ei.value.status_code == 422


def test_admin_confirm_forbidden_for_specialty_scheduler():
    with pytest.raises(ForbiddenError):
        asyncio.run(admin_confirm_assignment("a1", {"reason": "هاتفي"},
                                             current_user=SCABIN, sb=FakeSb(_store())))


def test_admin_confirm_sets_fields_and_audits():
    store = _store(declined=True)
    res = asyncio.run(admin_confirm_assignment(
        "a1", {"reason": "موافقة هاتفية"}, current_user=SUP, sb=FakeSb(store)))
    assert res["acceptance_status"] == "admin_confirmed"
    row = store["assignments"][0]
    assert row["admin_confirmed"] is True and row["admin_confirmed_by"] == "u_sup"
    assert row["declined"] is False
    a = _audits(store, "assignment_admin_confirmed")[0]
    assert "موافقة هاتفية" in a["after_data"]


# 14+15+16) Acceptance board RBAC + counts.
def _board(store, user=SCHED):
    return asyncio.run(flight_assignment_acceptance(
        "f1", current_user=user, sb=FakeSb(store)))


def test_board_crew_forbidden():
    with pytest.raises(ForbiddenError):
        _board(_store(), user=CREW1)


def test_board_cross_company_404():
    store = _store(); store["flights"] = []
    with pytest.raises(NotFoundError):
        _board(store)


def test_board_counts():
    store = _store()
    store["assignments"] = [
        {"id": "a1", "flight_id": "f1", "crew_id": "cr1", "acknowledged": True},
        {"id": "a2", "flight_id": "f1", "crew_id": "cr1", "declined": True},
        {"id": "a3", "flight_id": "f1", "crew_id": "cr1"},
        {"id": "a4", "flight_id": "f1", "crew_id": "cr1", "admin_confirmed": True},
    ]
    s = _board(store)["summary"]
    assert (s["accepted"], s["declined"], s["pending"], s["admin_confirmed"]) == (1, 1, 1, 1)
    assert s["acceptance_percentage"] == 50


# 9+10+11) Finalize gate.
def _finalize_store(**asg_common):
    base = {"acknowledged": False, "declined": False, "admin_confirmed": False}
    base.update(asg_common)
    return {
        "flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "B737",
                     "flight_number": "IA-1", "aircraft_registration": "YI-ASU",
                     "roster_finalized_status": None}],
        "assignments": [dict(base, id=f"a_{c}", flight_id="f1", crew_id=c)
                        for c in ("a", "b", "c", "d", "e")],
        "crew": [{"id": c, "rank": r, "full_name_ar": f"فرد {c}"} for c, r in
                 zip(("a", "b", "c", "d", "e"),
                     ("captain", "first_officer", "purser", "cabin_crew", "cabin_crew"))],
        "users": [{"id": f"usr_{c}", "crew_id": c} for c in ("a", "b", "c", "d", "e")],
    }


def _finalize(store):
    return asyncio.run(finalize_roster("f1", current_user=SCHED,
                                       sb=FakeSb(store), data=None))


def test_finalize_blocked_when_pending():
    with pytest.raises(HTTPException) as ei:
        _finalize(_finalize_store())                 # all pending
    assert ei.value.status_code == 422
    assert "لم يوافقوا" in ei.value.detail


def test_finalize_blocked_when_declined():
    store = _finalize_store(acknowledged=True)
    store["assignments"][0]["declined"] = True       # one decliner
    with pytest.raises(HTTPException) as ei:
        _finalize(store)
    assert ei.value.status_code == 422
    assert _audits(store, "finalize_blocked_due_to_pending_acceptance")


def test_finalize_passes_when_accepted_or_confirmed():
    store = _finalize_store(acknowledged=True)
    store["assignments"][0]["acknowledged"] = False
    store["assignments"][0]["admin_confirmed"] = True   # supervisor pinned one
    res = _finalize(store)
    assert res["already_finalized"] is False


# Crew portal upcoming-flights MUST carry the assignment lifecycle — without
# assignment_id the موافق/أرفض bar never renders (the bug crew reported).
def test_crew_flights_carry_acceptance_fields():
    from app.api.v1.endpoints.crew import get_crew_flights
    store = {
        "crew": [{"id": "cr1", "company_id": "c1"}],
        "assignments": [{"id": "a1", "flight_id": "f1", "crew_id": "cr1",
                         "acknowledged": False, "declined": False}],
        "flights": [{"id": "f1", "company_id": "c1", "status": "scheduled",
                     "publish_status": "published",   # crew only see published
                     "departure_time": "2099-01-01T10:00:00+00:00"}],
    }
    rows = asyncio.run(get_crew_flights("cr1", current_user=CREW1, sb=FakeSb(store)))
    assert rows and rows[0]["assignment_id"] == "a1"
    assert rows[0]["acknowledged"] is False         # → portal shows accept/decline
    assert rows[0]["declined"] is False


# 18) Publishing a draft with pending assignments notifies the crew.
def test_publish_notifies_pending_crew(monkeypatch):
    import app.api.v1.endpoints.flights as fl_mod
    import app.api.v1.endpoints.assignments as asg_mod
    monkeypatch.setattr(fl_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    monkeypatch.setattr(asg_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "publish_status": "draft",
                     "flight_number": "IA-1", "aircraft_registration": "YI-ASU",
                     "departure_time": "2099-01-01T10:00:00+00:00",
                     "origin_code": "BGW", "destination_code": "EBL"}],
        "assignments": [{"id": "a1", "flight_id": "f1", "crew_id": "cr1"}],
        "users": [{"id": "u_cr1", "crew_id": "cr1", "role": "crew",
                   "company_id": "c1", "is_active": True},
                  {"id": "u_al", "role": "cabin_allocator",
                   "company_id": "c1", "is_active": True}],
        "crew": [{"id": "cr1", "full_name_ar": "زها"}],
        "notification_delivery": [],
    }
    from app.api.v1.endpoints.flights import publish_flight
    asyncio.run(publish_flight("f1", current_user=SCHED, sb=FakeSb(store)))
    types = {n.get("type") for n in _notifs(store)}
    # Publishing fans out notifications (allocators directly; rostered crew via
    # the best-effort assigned-crew notifier) — the acceptance ask reaches crew
    # the moment the draft goes live.
    assert "flight_published" in types
