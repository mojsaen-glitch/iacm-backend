"""Scheduling audit-trail + unpublish governance (H1/H2/M1).

H1 — connected-duty audit must go to the REAL `audit_log` table (it was written
     to a non-existent `audit_logs` table → silently lost).
H2 — deleting an assignment must leave an audit entry: who / when / flight /
     crew / role / optional reason + a snapshot of the deleted row.
M1 — un-publishing is SUPERVISORY (roster approvers), publish stays open to
     specialty schedulers.

Run:  py -m pytest tests/test_scheduling_audit.py -q
"""
import asyncio
import inspect
import json

import pytest

import app.api.v1.endpoints.assignments as asg_mod
from app.api.v1.endpoints.assignments import remove_assignment
from app.api.v1.endpoints.flights import unpublish_flight, publish_flight
from app.core.exceptions import ForbiddenError


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def insert(self, p): self.store.setdefault(self.name + "_inserts", []).append(p); return self
    def update(self, p): self.store.setdefault(self.name + "_updates", []).append(p); return self
    def delete(self): self.store.setdefault(self.name + "_deletes", []).append(True); return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


SCHEDULER  = {"id": "u1", "role": "scheduler",  "company_id": "c1", "is_superuser": False}
SCHED_CAB  = {"id": "u2", "role": "sched_cabin", "company_id": "c1", "is_superuser": False}


def _audits(store, action):
    return [a for a in store.get("audit_log_inserts", []) if a.get("action") == action]


# ── H1: connected-duty audit table (source-level regression guard) ────────────
def test_connected_duty_audits_to_real_table():
    src = inspect.getsource(asg_mod)
    assert 'table("audit_logs")' not in src, \
        "connected-duty audit must use audit_log (singular) — audit_logs does not exist"
    assert '"assign_connected_duty"' in src


# ── H2: remove_assignment audit ───────────────────────────────────────────────
def _removal_store():
    return {
        "assignments": [{"id": "a1", "flight_id": "f1", "crew_id": "cr1",
                         "duty_type": "operating", "assigned_role": "cabin_crew",
                         "assigned_by": "u9", "created_at": "2026-06-01T00:00:00Z"}],
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-229",
                     "origin_code": "BGW", "destination_code": "EBL"}],
        "crew": [{"id": "cr1", "full_name_ar": "زها سمير", "rank": "cabin_crew"}],
        "users": [],
    }


def test_remove_assignment_writes_audit_snapshot():
    store = _removal_store()
    asyncio.run(remove_assignment("a1", current_user=SCHEDULER, sb=FakeSb(store),
                                  reason="مرض الفرد"))
    assert store.get("assignments_deletes")
    audits = _audits(store, "remove_assignment")
    assert len(audits) == 1
    a = audits[0]
    assert a["user_id"] == "u1" and a["entity_id"] == "a1" and a["created_at"]
    d = json.loads(a["after_data"])
    assert d["flight_number"] == "IA-229"
    assert d["crew_name"] == "زها سمير"
    assert d["rank"] == "cabin_crew"
    assert d["duty_type"] == "operating"
    assert d["reason"] == "مرض الفرد"
    assert d["deleted_assignment"]["id"] == "a1"          # row snapshot kept
    assert d["deleted_assignment"]["assigned_by"] == "u9"


def test_remove_assignment_audit_without_reason():
    store = _removal_store()
    asyncio.run(remove_assignment("a1", current_user=SCHEDULER, sb=FakeSb(store),
                                  reason=None))
    d = json.loads(_audits(store, "remove_assignment")[0]["after_data"])
    assert d["reason"] is None


# ── M1: unpublish is supervisory; publish stays open to sched_* ───────────────
def test_unpublish_forbidden_for_specialty_scheduler():
    store = {"flights": [{"id": "f1", "company_id": "c1", "publish_status": "published"}]}
    with pytest.raises(ForbiddenError):
        asyncio.run(unpublish_flight("f1", current_user=SCHED_CAB, sb=FakeSb(store)))
    assert "flights_updates" not in store


def test_unpublish_allowed_for_scheduler():
    store = {"flights": [{"id": "f1", "company_id": "c1", "publish_status": "published"}]}
    asyncio.run(unpublish_flight("f1", current_user=SCHEDULER, sb=FakeSb(store)))
    assert store["flights_updates"][0]["publish_status"] == "draft"


def test_publish_writes_audit(monkeypatch):
    import app.api.v1.endpoints.flights as fl_mod
    monkeypatch.setattr(fl_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "publish_status": "draft",
                     "flight_number": "IA-229", "aircraft_registration": "YI-ASU",
                     "departure_time": "2026-06-20T10:00:00+00:00",
                     "origin_code": "BGW", "destination_code": "EBL"}],
        "users": [],
        # Full mandatory complement — publish now enforces the min-crew gate.
        "assignments": [{"id": f"a{i}", "flight_id": "f1", "crew_id": c,
                         "duty_type": "operating"}
                        for i, c in enumerate(("p1", "p2", "cc1", "cc2", "cc3"))],
        "crew": [{"id": "p1", "rank": "captain"},
                 {"id": "p2", "rank": "first_officer"},
                 {"id": "cc1", "rank": "cabin_crew"},
                 {"id": "cc2", "rank": "cabin_crew"},
                 {"id": "cc3", "rank": "cabin_crew"}],
    }
    asyncio.run(publish_flight("f1", current_user=SCHEDULER, sb=FakeSb(store)))
    audits = _audits(store, "publish_flight")
    assert len(audits) == 1
    assert json.loads(audits[0]["before_data"])["publish_status"] == "draft"
    after = json.loads(audits[0]["after_data"])
    assert after["publish_status"] == "published" and after["flight_number"] == "IA-229"


def test_unpublish_writes_audit_and_notifies_crew(monkeypatch):
    import app.api.v1.endpoints.flights as fl_mod
    monkeypatch.setattr(fl_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "publish_status": "published",
                     "flight_number": "IA-229", "origin_code": "BGW",
                     "destination_code": "EBL"}],
        "assignments": [{"crew_id": "cr1"}, {"crew_id": "cr2"}],
        "users": [{"id": "u_cr1", "crew_id": "cr1"}, {"id": "u_cr2", "crew_id": "cr2"}],
    }
    asyncio.run(unpublish_flight("f1", current_user=SCHEDULER, sb=FakeSb(store)))
    audits = _audits(store, "unpublish_flight")
    assert len(audits) == 1
    assert json.loads(audits[0]["before_data"])["publish_status"] == "published"
    assert json.loads(audits[0]["after_data"])["publish_status"] == "draft"
    # Assigned crew must be told the flight was withdrawn (it vanishes from
    # their portal) — assignments themselves are untouched.
    notifs = []
    for b in store.get("notifications_inserts", []):
        notifs.extend(b if isinstance(b, list) else [b])
    assert len(notifs) == 2
    assert all(n["type"] == "flight_unpublished" for n in notifs)
    assert "assignments_deletes" not in store


def test_unpublish_idempotent_no_audit_when_already_draft():
    store = {"flights": [{"id": "f1", "company_id": "c1", "publish_status": "draft"}]}
    asyncio.run(unpublish_flight("f1", current_user=SCHEDULER, sb=FakeSb(store)))
    assert not _audits(store, "unpublish_flight")     # no-op → no audit noise


def test_publish_still_allowed_for_specialty_scheduler(monkeypatch):
    import app.api.v1.endpoints.flights as fl_mod
    monkeypatch.setattr(fl_mod.push_service, "send_to_users", lambda *a, **k: {"attempted": 0})
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "publish_status": "draft",
                     "flight_number": "IA-1", "aircraft_registration": "YI-ASU",
                     "departure_time": "2026-06-20T10:00:00+00:00",
                     "origin_code": "BGW", "destination_code": "EBL"}],
        "users": [],
        # Full mandatory complement — publish now enforces the min-crew gate.
        "assignments": [{"id": f"a{i}", "flight_id": "f1", "crew_id": c,
                         "duty_type": "operating"}
                        for i, c in enumerate(("p1", "p2", "cc1", "cc2", "cc3"))],
        "crew": [{"id": "p1", "rank": "captain"},
                 {"id": "p2", "rank": "first_officer"},
                 {"id": "cc1", "rank": "cabin_crew"},
                 {"id": "cc2", "rank": "cabin_crew"},
                 {"id": "cc3", "rank": "cabin_crew"}],
    }
    res = asyncio.run(publish_flight("f1", current_user=SCHED_CAB, sb=FakeSb(store)))
    assert store["flights_updates"][0]["publish_status"] == "published" or res
