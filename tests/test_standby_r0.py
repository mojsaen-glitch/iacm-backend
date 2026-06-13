"""Reserve/Standby — R0 (governance: audit every action + clean expiry).

R0 adds NO operational logic (no callout notifications, no escalation, no
assignment bridge). It only:
  • writes a standard `write_audit` row for create / callout / cancel / delete,
  • expires ACTIVE reserves whose window has ended (sweep → EXPIRED, audited),
  • keeps cancelled / called-out / expired reserves out of `suggest`.

Run:  py -m pytest tests/test_standby_r0.py -q
"""
import asyncio
import json

import pytest

import app.api.v1.endpoints.standby as standby_mod
from app.api.v1.endpoints.standby import (
    create_standby, callout_standby, cancel_standby, delete_standby,
    suggest_standby, expire_standby_now, cron_expire_standby,
    _expire_company_standby,
)
from app.core.exceptions import ForbiddenError


# ── Filtering + mutating fake: honours .eq()/.in_() so company isolation and
#    idempotency are real, not assumed. select() returns matching rows. ────────
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


ADMIN = {"id": "u1", "name_ar": "مدير العمليات", "role": "admin",
         "company_id": "c1", "is_superuser": False}
SCHED = {"id": "u2", "name_ar": "مجدول", "role": "scheduler",
         "company_id": "c1", "is_superuser": False}

PAST   = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


def _audits(store, action):
    return [a for a in store.get("audit_log", []) if a.get("action") == action]


# ── 1) every sensitive action writes a full before/after audit row ────────────
def test_create_writes_audit():
    store = {"crew": [{"id": "cr1", "company_id": "c1"}], "standby_assignments": []}
    asyncio.run(create_standby(
        {"crew_id": "cr1", "standby_type": "HOME_STANDBY",
         "start_time": FUTURE, "end_time": FUTURE, "response_minutes": 90},
        current_user=ADMIN, sb=FakeSb(store)))
    a = _audits(store, "standby_created")
    assert len(a) == 1 and a[0]["company_id"] == "c1"
    after = json.loads(a[0]["after_data"])
    assert after["crew_id"] == "cr1" and after["standby_type"] == "HOME_STANDBY"
    assert after["response_minutes"] == 90


def test_callout_writes_before_after_audit():
    store = {"standby_assignments": [
        {"id": "s1", "company_id": "c1", "status": "ACTIVE", "called_out": False,
         "assigned_flight_id": None}]}
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    a = _audits(store, "standby_called_out")
    assert len(a) == 1
    before, after = json.loads(a[0]["before_data"]), json.loads(a[0]["after_data"])
    assert before["status"] == "ACTIVE" and before["called_out"] is False
    assert after["status"] == "ASSIGNED" and after["assigned_flight_id"] == "f1"
    # row actually moved out of ACTIVE
    assert store["standby_assignments"][0]["status"] == "ASSIGNED"


def test_cancel_writes_before_after_audit():
    store = {"standby_assignments": [
        {"id": "s1", "company_id": "c1", "status": "ACTIVE"}]}
    asyncio.run(cancel_standby("s1", current_user=ADMIN, sb=FakeSb(store)))
    a = _audits(store, "standby_cancelled")
    assert len(a) == 1
    assert json.loads(a[0]["before_data"])["status"] == "ACTIVE"
    assert json.loads(a[0]["after_data"])["status"] == "CANCELLED"
    assert store["standby_assignments"][0]["status"] == "CANCELLED"


def test_delete_snapshots_row_in_audit():
    store = {"standby_assignments": [
        {"id": "s1", "company_id": "c1", "status": "ACTIVE", "crew_id": "cr1",
         "standby_type": "AIRPORT_STANDBY", "start_time": PAST, "end_time": FUTURE}]}
    asyncio.run(delete_standby("s1", current_user=ADMIN, sb=FakeSb(store)))
    a = _audits(store, "standby_deleted")
    assert len(a) == 1
    snap = json.loads(a[0]["after_data"])["deleted_standby"]
    assert snap["crew_id"] == "cr1" and snap["standby_type"] == "AIRPORT_STANDBY"
    # row is gone
    assert store["standby_assignments"] == []


# ── 2 & 3) cancelled / called-out / expired reserves never enter suggest ──────
class _StubEngine:
    """Stub the compliance engine so suggest's CANDIDATE FILTER is what's tested,
    not the full FDP machinery."""
    def __init__(self, sb): pass
    def check_crew(self, **k): return {"status": "OK", "blocking_reasons": []}


def test_suggest_excludes_cancelled_calledout_and_expired(monkeypatch):
    monkeypatch.setattr(standby_mod, "ComplianceEngine", _StubEngine)
    store = {
        "flights": [{"id": "f1", "company_id": "c1",
                     "departure_time": "2099-06-01T10:00:00+00:00",
                     "arrival_time": "2099-06-01T12:00:00+00:00",
                     "origin_code": "BGW", "destination_code": "EBL",
                     "aircraft_type": "A320"}],
        "crew": [{"id": c, "company_id": "c1"} for c in
                 ("cr_ok", "cr_cancel", "cr_called", "cr_expired")],
        "standby_assignments": [
            {"id": "ok", "company_id": "c1", "status": "ACTIVE", "crew_id": "cr_ok",
             "start_time": "2099-06-01T08:00:00+00:00",
             "end_time": "2099-06-01T18:00:00+00:00", "response_minutes": 30},
            {"id": "x1", "company_id": "c1", "status": "CANCELLED", "crew_id": "cr_cancel",
             "start_time": "2099-06-01T08:00:00+00:00",
             "end_time": "2099-06-01T18:00:00+00:00", "response_minutes": 30},
            {"id": "x2", "company_id": "c1", "status": "CALLED_OUT", "crew_id": "cr_called",
             "start_time": "2099-06-01T08:00:00+00:00",
             "end_time": "2099-06-01T18:00:00+00:00", "response_minutes": 30},
            {"id": "x3", "company_id": "c1", "status": "ACTIVE", "crew_id": "cr_expired",
             "start_time": PAST, "end_time": PAST, "response_minutes": 30},
        ],
    }
    res = asyncio.run(suggest_standby("f1", current_user=ADMIN, sb=FakeSb(store)))
    crew_ids = {c["crew_id"] for c in res["candidates"]}
    assert crew_ids == {"cr_ok"}


# ── 4 & 5) expiry sweep: only ended ACTIVE → EXPIRED, isolated, idempotent ────
def _sweep_store():
    return {"standby_assignments": [
        {"id": "a1", "company_id": "c1", "status": "ACTIVE",   "end_time": PAST,
         "crew_id": "cr1"},   # expires
        {"id": "a2", "company_id": "c1", "status": "ACTIVE",   "end_time": FUTURE,
         "crew_id": "cr2"},   # stays
        {"id": "a3", "company_id": "c1", "status": "CANCELLED", "end_time": PAST,
         "crew_id": "cr3"},   # not ACTIVE → untouched
        {"id": "b1", "company_id": "c2", "status": "ACTIVE",   "end_time": PAST,
         "crew_id": "cr4"},   # other company → untouched when sweeping c1
    ]}


def test_expiry_sweep_only_ended_active_and_company_isolated():
    store = _sweep_store()
    res = _expire_company_standby(FakeSb(store), "c1",
                                  {"id": "system", "company_id": "c1"})
    assert res["expired"] == 1 and res["ids"] == ["a1"]
    by_id = {r["id"]: r for r in store["standby_assignments"]}
    assert by_id["a1"]["status"] == "EXPIRED"
    assert by_id["a2"]["status"] == "ACTIVE"        # future window untouched
    assert by_id["a3"]["status"] == "CANCELLED"     # non-ACTIVE untouched
    assert by_id["b1"]["status"] == "ACTIVE"        # other company untouched
    a = _audits(store, "standby_expired")
    assert len(a) == 1 and a[0]["entity_id"] == "a1" and a[0]["company_id"] == "c1"


def test_expiry_sweep_is_idempotent():
    store = _sweep_store()
    sb = FakeSb(store)
    _expire_company_standby(sb, "c1", {"id": "system", "company_id": "c1"})
    second = _expire_company_standby(sb, "c1", {"id": "system", "company_id": "c1"})
    assert second["expired"] == 0          # nothing left to expire
    assert len(_audits(store, "standby_expired")) == 1   # no duplicate audit


def test_expiry_does_not_break_listing():
    store = _sweep_store()
    _expire_company_standby(FakeSb(store), "c1", {"id": "system", "company_id": "c1"})
    # The c1 rows are still present (one now EXPIRED) — list is intact, not emptied.
    c1_rows = [r for r in store["standby_assignments"] if r["company_id"] == "c1"]
    assert len(c1_rows) == 3
    assert any(r["status"] == "EXPIRED" for r in c1_rows)


# ── governance on the manual / cron triggers ─────────────────────────────────
def test_manual_expire_requires_supervisor():
    store = _sweep_store()
    with pytest.raises(ForbiddenError):
        asyncio.run(expire_standby_now(current_user=SCHED, sb=FakeSb(store)))
    # scheduler is NOT a supervisor → nothing expired
    assert all(r["status"] != "EXPIRED" for r in store["standby_assignments"])


def test_manual_expire_allowed_for_admin():
    store = _sweep_store()
    res = asyncio.run(expire_standby_now(current_user=ADMIN, sb=FakeSb(store)))
    assert res["expired"] == 1


def test_cron_expire_rejects_missing_secret():
    with pytest.raises(ForbiddenError):
        asyncio.run(cron_expire_standby(sb=FakeSb({}), authorization=None))
