"""Reserve/Standby — R3 (auto-escalation on no-response / rejection).

A called-out reserve that REJECTS or does NOT answer within its response
window is escalated to the next valid candidate — a callout ONLY (acceptance +
assignment still flow through R2 → /assignments). The sweep is idempotent
(a processed failed-callout is stamped `escalated_at`), respects company scope,
never re-picks a reserve already called for the flight, and alerts ops when no
candidate remains.

Run:  py -m pytest tests/test_standby_r3.py -q
"""
import asyncio

import pytest

import app.api.v1.endpoints.standby as standby_mod
from app.api.v1.endpoints.standby import (
    _escalate_company_standby, escalate_standby_now, cron_escalate_standby,
)
from app.core.exceptions import ForbiddenError


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


ADMIN = {"id": "u1", "name_ar": "مدير", "role": "admin",
         "company_id": "c1", "is_superuser": False}
SCHED = {"id": "u2", "role": "scheduler", "company_id": "c1", "is_superuser": False}

PAST = "2000-01-01T00:00:00+00:00"      # called_out_at well in the past → timed out
FUTURE = "2099-06-01T00:00:00+00:00"


class _OkEngine:
    """Every candidate is compliant — isolates the escalation logic."""
    def __init__(self, sb): pass
    def check_crew(self, **k): return {"status": "OK", "blocking_reasons": []}


def _patch_engine(monkeypatch):
    monkeypatch.setattr(standby_mod, "ComplianceEngine", _OkEngine)


def _rec_push(monkeypatch):
    monkeypatch.setattr(standby_mod.push_service, "send_to_users",
                        lambda *a, **k: {"attempted": 0})


def _audits(store, action):
    return [a for a in store.get("audit_log", []) if a.get("action") == action]


def _flight():
    return {"id": "f1", "company_id": "c1", "flight_number": "IA-560",
            "departure_time": "2099-06-01T10:00:00+00:00",
            "arrival_time": "2099-06-01T12:00:00+00:00",
            "origin_code": "BGW", "destination_code": "EBL", "aircraft_type": "A320"}


def _failed_callout(crew="cr1", response=None, called_at=PAST, resp_min=60):
    """A called-out reserve that has failed (no response past deadline, or
    rejected). status ASSIGNED mirrors a callout-with-flight; assignment_id is
    None so it is NOT a real R2 tasking."""
    return {"id": "s_failed", "company_id": "c1", "crew_id": crew,
            "status": "ASSIGNED", "called_out": True, "assigned_flight_id": "f1",
            "called_out_at": called_at, "response_minutes": resp_min,
            "response_status": response, "assignment_id": None,
            "escalated_at": None, "escalation_status": None,
            "start_time": "2099-06-01T08:00:00+00:00",
            "end_time": "2099-06-01T18:00:00+00:00", "airport_code": "BGW"}


def _candidate(cid="s_next", crew="cr2"):
    """A fresh ACTIVE reserve eligible for f1 (window covers departure)."""
    return {"id": cid, "company_id": "c1", "crew_id": crew, "status": "ACTIVE",
            "called_out": False, "assigned_flight_id": None,
            "response_minutes": 30, "response_status": None, "assignment_id": None,
            "escalated_at": None,
            "start_time": "2099-06-01T08:00:00+00:00",
            "end_time": "2099-06-01T18:00:00+00:00", "airport_code": "BGW"}


def _store(failed, candidates):
    return {
        "standby_assignments": [failed, *candidates],
        "flights": [_flight()],
        "users": [{"id": "sched1", "role": "scheduler", "company_id": "c1",
                   "is_active": True}],
        "notifications": [],
    }


# ── 1) timed-out (no-response) callout escalates to next candidate ───────────
def test_timeout_escalates_to_next_candidate(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response=None, called_at=PAST), [_candidate()])
    res = _escalate_company_standby(FakeSb(store), "c1", ADMIN)
    assert res["escalated"] == 1 and res["exhausted"] == 0
    by_id = {r["id"]: r for r in store["standby_assignments"]}
    assert by_id["s_failed"]["escalation_status"] == "ESCALATED"
    assert by_id["s_failed"]["escalated_at"]
    assert by_id["s_next"]["called_out"] is True            # next reserve called out
    assert by_id["s_next"]["assigned_flight_id"] == "f1"
    assert _audits(store, "standby_escalated")


# ── 2) rejected callout escalates ────────────────────────────────────────────
def test_rejected_escalates_to_next_candidate(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    # Rejected, but called_out_at is recent → only the rejection triggers it.
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE),
                   [_candidate()])
    res = _escalate_company_standby(FakeSb(store), "c1", ADMIN)
    assert res["escalated"] == 1
    assert {r["id"]: r for r in store["standby_assignments"]}["s_next"]["called_out"] is True


# ── 3) a reserve that is not timed-out and not rejected is left alone ─────────
def test_in_window_unanswered_is_not_escalated(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    # called_out_at in the FUTURE → deadline not reached → no escalation.
    store = _store(_failed_callout(response=None, called_at=FUTURE), [_candidate()])
    res = _escalate_company_standby(FakeSb(store), "c1", ADMIN)
    assert res["escalated"] == 0 and res["exhausted"] == 0
    assert {r["id"]: r for r in store["standby_assignments"]}["s_next"]["called_out"] is False


# ── 3b) a real R2 acceptance (assignment_id set) is never escalated ──────────
def test_real_assignment_is_never_escalated(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    failed = _failed_callout(response="ACCEPTED", called_at=PAST)
    failed["assignment_id"] = "a_done"          # truly tasked
    store = _store(failed, [_candidate()])
    res = _escalate_company_standby(FakeSb(store), "c1", ADMIN)
    assert res["escalated"] == 0 and res["exhausted"] == 0


# ── 4) idempotent: a second sweep does nothing new ───────────────────────────
def test_escalation_is_idempotent(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE),
                   [_candidate()])
    sb = FakeSb(store)
    _escalate_company_standby(sb, "c1", ADMIN)
    second = _escalate_company_standby(sb, "c1", ADMIN)
    assert second["escalated"] == 0 and second["exhausted"] == 0
    assert len(_audits(store, "standby_escalated")) == 1     # no duplicate audit
    # the escalated candidate (now CALLED_OUT/ASSIGNED) is never re-picked
    assert len([r for r in store["standby_assignments"]
                if r.get("called_out")]) == 2


# ── 5) no candidate left → ops alert (exhausted) ─────────────────────────────
def test_exhausted_alerts_ops(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE), [])
    res = _escalate_company_standby(FakeSb(store), "c1", ADMIN)
    assert res["escalated"] == 0 and res["exhausted"] == 1
    by_id = {r["id"]: r for r in store["standby_assignments"]}
    assert by_id["s_failed"]["escalation_status"] == "EXHAUSTED"
    assert _audits(store, "standby_escalation_exhausted")
    # scheduler/ops got an in-app notification
    assert any(n["type"] == "standby_response" for n in store["notifications"])


def test_exhausted_does_not_realert_on_rerun(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE), [])
    sb = FakeSb(store)
    _escalate_company_standby(sb, "c1", ADMIN)
    _escalate_company_standby(sb, "c1", ADMIN)
    assert len(_audits(store, "standby_escalation_exhausted")) == 1
    notifs = [n for n in store["notifications"] if n["type"] == "standby_response"]
    assert len(notifs) == 1                                  # alerted exactly once


# ── 6) company scope ─────────────────────────────────────────────────────────
def test_escalation_respects_company_scope(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE),
                   [_candidate()])
    # A foreign-company failed callout must be untouched when sweeping c1.
    foreign = _failed_callout(crew="cr9", response="REJECTED", called_at=FUTURE)
    foreign["id"] = "s_foreign"; foreign["company_id"] = "c2"
    store["standby_assignments"].append(foreign)
    _escalate_company_standby(FakeSb(store), "c1", ADMIN)
    by_id = {r["id"]: r for r in store["standby_assignments"]}
    assert by_id["s_foreign"]["escalated_at"] is None        # c2 untouched


# ── 7) audit for escalation + result ─────────────────────────────────────────
def test_escalation_writes_audit_with_trigger_and_target(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE),
                   [_candidate()])
    _escalate_company_standby(FakeSb(store), "c1", ADMIN)
    a = _audits(store, "standby_escalated")[0]
    import json
    after = json.loads(a["after_data"])
    assert after["trigger"] == "rejected"
    assert after["next_standby_id"] == "s_next" and after["next_crew_id"] == "cr2"
    assert a["company_id"] == "c1"


# ── governance on triggers ───────────────────────────────────────────────────
def test_manual_escalate_requires_supervisor(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE),
                   [_candidate()])
    with pytest.raises(ForbiddenError):
        asyncio.run(escalate_standby_now(current_user=SCHED, sb=FakeSb(store)))


def test_manual_escalate_allowed_for_admin(monkeypatch):
    _patch_engine(monkeypatch); _rec_push(monkeypatch)
    store = _store(_failed_callout(response="REJECTED", called_at=FUTURE),
                   [_candidate()])
    res = asyncio.run(escalate_standby_now(current_user=ADMIN, sb=FakeSb(store)))
    assert res["escalated"] == 1


def test_cron_escalate_rejects_missing_secret():
    with pytest.raises(ForbiddenError):
        asyncio.run(cron_escalate_standby(sb=FakeSb({}), authorization=None))
