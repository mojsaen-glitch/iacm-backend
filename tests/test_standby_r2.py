"""Reserve/Standby — R2 (crew accept/reject + assignment bridge).

The accepted callout is turned into an assignment ONLY through the existing
`assign_crew` path (no parallel route), so every safety gate + the existing
assignment audit apply. Here we monkeypatch `assign_crew` to prove the BRIDGE
behaviour (it is invoked with the right payload + a privileged assigner, its
failure is surfaced, retries don't duplicate) — the gates themselves are
covered by the assignments suite.

Run:  py -m pytest tests/test_standby_r2.py -q
"""
import asyncio

import pytest
from fastapi import HTTPException

import app.api.v1.endpoints.standby as standby_mod
import app.api.v1.endpoints.assignments as assignments_mod
from app.api.v1.endpoints.standby import respond_standby
from app.core.exceptions import ForbiddenError, NotFoundError, ConflictError


# ── filtering + mutating fake (honours .eq()/.in_()) ─────────────────────────
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): self._op = "select"; return self
    def eq(self, f, v): self._filters.append((f, v)); return self
    def in_(self, f, vals): self._filters.append((f, list(vals))); return self
    def order(self, *a, **k): return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self
    def delete(self): self._op = "delete"; return self

    def _match(self, r):
        for f, v in self._filters:
            if isinstance(v, list):
                if r.get(f) not in v:
                    return False
            elif r.get(f) != v:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            return _R([dict(i) for i in items])
        hits = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in hits:
                r.update(self._payload)
            return _R([dict(r) for r in hits])
        if self._op == "delete":
            self.store[self.name] = [r for r in rows if not self._match(r)]
            return _R([dict(r) for r in hits])
        return _R([dict(r) for r in hits])


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


CREW = {"id": "u_crew", "name_ar": "طيار", "role": "crew",
        "crew_id": "cr1", "company_id": "c1"}


def _store(status="CALLED_OUT", response=None, assignment_id=None,
           with_assigner=True):
    s = {
        "standby_assignments": [{
            "id": "s1", "company_id": "c1", "crew_id": "cr1", "status": status,
            "called_out": True, "assigned_flight_id": "f1", "created_by": "sched1",
            "response_status": response, "response_reason": None,
            "responded_at": None, "assignment_id": assignment_id,
            "assignment_error": None, "airport_code": "BGW",
            "start_time": "2099-06-01T08:00:00+00:00",
            "end_time": "2099-06-01T18:00:00+00:00"}],
        "users": [], "assignments": [], "notifications": [],
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-560"}],
    }
    if with_assigner:
        s["users"].append({"id": "sched1", "role": "scheduler", "company_id": "c1",
                           "is_active": True, "name_ar": "مجدول"})
    return s


def _patch_assign(monkeypatch, result=None):
    calls = []

    async def fake(data, current_user, sb):
        calls.append({"data": data, "user": current_user})
        sb.table("assignments").insert(
            {"id": "a_new", "flight_id": data["flight_id"],
             "crew_id": data["crew_id"]}).execute()
        return result or {"id": "a_new"}

    monkeypatch.setattr(assignments_mod, "assign_crew", fake)
    return calls


def _patch_assign_raises(monkeypatch, exc):
    async def fake(data, current_user, sb):
        raise exc
    monkeypatch.setattr(assignments_mod, "assign_crew", fake)


def _rec_push(monkeypatch):
    monkeypatch.setattr(standby_mod.push_service, "send_to_users",
                        lambda *a, **k: {"attempted": 0})


def _audits(store, action):
    return [a for a in store.get("audit_log", []) if a.get("action") == action]


# ── 1 & 3) accept goes through the EXISTING assign path + links result ───────
def test_accept_uses_existing_assign_path_and_links(monkeypatch):
    calls = _patch_assign(monkeypatch)
    _rec_push(monkeypatch)
    store = _store()
    res = asyncio.run(respond_standby("s1", {"action": "accept"},
                                      current_user=CREW, sb=FakeSb(store)))
    assert res["response_status"] == "ACCEPTED" and res["assignment_id"] == "a_new"
    # the SAME assign_crew path, with the right payload (gates run inside it)...
    assert len(calls) == 1
    assert calls[0]["data"] == {"flight_id": "f1", "crew_id": "cr1",
                                "duty_type": "operating"}
    # ...performed by the standby owner (a scheduler), never the crew member.
    assert calls[0]["user"]["id"] == "sched1"
    row = store["standby_assignments"][0]
    assert row["assignment_id"] == "a_new" and row["status"] == "ASSIGNED"


# ── 2) crew may only respond to their OWN callout, own company ───────────────
def test_crew_cannot_respond_for_another_crew(monkeypatch):
    _patch_assign(monkeypatch); _rec_push(monkeypatch)
    store = _store()
    with pytest.raises(ForbiddenError):
        asyncio.run(respond_standby("s1", {"action": "accept"},
                                    current_user={**CREW, "crew_id": "cr2"},
                                    sb=FakeSb(store)))


def test_cross_company_response_blocked(monkeypatch):
    _patch_assign(monkeypatch); _rec_push(monkeypatch)
    store = _store()
    with pytest.raises(NotFoundError):
        asyncio.run(respond_standby("s1", {"action": "accept"},
                                    current_user={**CREW, "company_id": "c2"},
                                    sb=FakeSb(store)))


# ── 4) assignment failure after accept is visible — no phantom tasking ───────
def test_assignment_failure_is_visible_and_not_a_tasking(monkeypatch):
    _patch_assign_raises(monkeypatch, ForbiddenError("اكتمل عدد الطيارين"))
    _rec_push(monkeypatch)
    store = _store()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(respond_standby("s1", {"action": "accept"},
                                    current_user=CREW, sb=FakeSb(store)))
    assert ei.value.status_code == 409
    row = store["standby_assignments"][0]
    assert row["assignment_error"]                 # failure recorded + visible
    assert row["assignment_id"] is None            # NOT tasked
    assert row["response_status"] == "ACCEPTED"    # acceptance still recorded
    assert _audits(store, "standby_assign_failed")


# ── 5) reject requires a reason and stores it ────────────────────────────────
def test_reject_requires_reason(monkeypatch):
    _rec_push(monkeypatch)
    store = _store()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(respond_standby("s1", {"action": "reject"},
                                    current_user=CREW, sb=FakeSb(store)))
    assert ei.value.status_code == 422


def test_reject_stores_reason_and_audits(monkeypatch):
    _rec_push(monkeypatch)
    store = _store()
    res = asyncio.run(respond_standby("s1", {"action": "reject", "reason": "مرتبط بواجب"},
                                      current_user=CREW, sb=FakeSb(store)))
    assert res["response_status"] == "REJECTED"
    row = store["standby_assignments"][0]
    assert row["response_status"] == "REJECTED" and row["response_reason"] == "مرتبط بواجب"
    assert _audits(store, "standby_response")


# ── 6) retries don't duplicate the assignment / response ─────────────────────
def test_retry_accept_does_not_duplicate(monkeypatch):
    calls = _patch_assign(monkeypatch)
    _rec_push(monkeypatch)
    store = _store()
    sb = FakeSb(store)
    asyncio.run(respond_standby("s1", {"action": "accept"}, current_user=CREW, sb=sb))
    asyncio.run(respond_standby("s1", {"action": "accept"}, current_user=CREW, sb=sb))
    assert len(calls) == 1                          # 2nd accept is idempotent
    assert len(store["assignments"]) == 1           # exactly one assignment


def test_conflict_links_existing_assignment(monkeypatch):
    # assign_crew raises ConflictError (crew already on the flight) — respond
    # must LINK the existing assignment, not create a duplicate.
    _patch_assign_raises(monkeypatch, ConflictError("already assigned"))
    _rec_push(monkeypatch)
    store = _store()
    store["assignments"].append({"id": "a_existing", "flight_id": "f1",
                                 "crew_id": "cr1"})
    res = asyncio.run(respond_standby("s1", {"action": "accept"},
                                      current_user=CREW, sb=FakeSb(store)))
    assert res.get("idempotent") is True and res["assignment_id"] == "a_existing"
    assert len(store["assignments"]) == 1           # no duplicate


def test_reject_is_idempotent(monkeypatch):
    _rec_push(monkeypatch)
    store = _store()
    sb = FakeSb(store)
    asyncio.run(respond_standby("s1", {"action": "reject", "reason": "الأول"},
                                current_user=CREW, sb=sb))
    res = asyncio.run(respond_standby("s1", {"action": "reject", "reason": "الثاني"},
                                      current_user=CREW, sb=sb))
    assert res["response_status"] == "REJECTED"
    assert len(_audits(store, "standby_response")) == 1   # no duplicate audit
    assert store["standby_assignments"][0]["response_reason"] == "الأول"  # first kept


# ── 7) audit for both accept and reject ──────────────────────────────────────
def test_accept_writes_response_and_assigned_audit(monkeypatch):
    _patch_assign(monkeypatch); _rec_push(monkeypatch)
    store = _store()
    asyncio.run(respond_standby("s1", {"action": "accept"},
                                current_user=CREW, sb=FakeSb(store)))
    actions = [a["action"] for a in store.get("audit_log", [])]
    assert "standby_response" in actions and "standby_assigned" in actions


# ── guards: no active callout / switching answer ─────────────────────────────
def test_cannot_respond_without_active_callout(monkeypatch):
    _patch_assign(monkeypatch); _rec_push(monkeypatch)
    store = _store(status="ACTIVE")
    store["standby_assignments"][0]["called_out"] = False
    with pytest.raises(HTTPException) as ei:
        asyncio.run(respond_standby("s1", {"action": "accept"},
                                    current_user=CREW, sb=FakeSb(store)))
    assert ei.value.status_code == 409


def test_accept_after_reject_conflicts(monkeypatch):
    _patch_assign(monkeypatch); _rec_push(monkeypatch)
    store = _store(response="REJECTED")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(respond_standby("s1", {"action": "accept"},
                                    current_user=CREW, sb=FakeSb(store)))
    assert ei.value.status_code == 409
