"""Reserve/Standby — R5 (OCC coverage + IROPS unification).

READ-ONLY surfacing: a coverage endpoint shows the available reserve pool (same
/suggest ranking) plus every reserve engaged for the flight with its state, and
IROPS recovery-options now reads the managed standby pool FIRST. Neither path
assigns — acceptance + assignment stay in R2 → /assignments.

Run:  py -m pytest tests/test_standby_r5.py -q
"""
import asyncio
from datetime import datetime, timezone

import app.api.v1.endpoints.standby as standby_mod
import app.api.v1.endpoints.irops as irops_mod
from app.api.v1.endpoints.standby import standby_coverage, _standby_state
from app.api.v1.endpoints.irops import recovery_options


# ── filtering fake (eq/in_ honoured; range ops pass through) ─────────────────
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): self._op = "select"; return self
    def eq(self, f, v): self._filters.append((f, v)); return self
    def in_(self, f, vals): self._filters.append((f, list(vals))); return self
    # range / misc filters are pass-through no-ops for these tests
    def gte(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def gt(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
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


ADMIN = {"id": "u1", "role": "admin", "company_id": "c1", "is_superuser": False}
NOW = datetime(2099, 6, 1, 9, 0, tzinfo=timezone.utc)


class _OkEngine:
    def __init__(self, sb): pass
    def check_crew(self, **k): return {"status": "OK", "blocking_reasons": []}


def _patch_engine(monkeypatch):
    monkeypatch.setattr(standby_mod, "ComplianceEngine", _OkEngine)


def _flight():
    return {"id": "f1", "company_id": "c1", "flight_number": "IA-560",
            "departure_time": "2099-06-01T10:00:00+00:00",
            "arrival_time": "2099-06-01T12:00:00+00:00",
            "origin_code": "BGW", "destination_code": "EBL", "aircraft_type": "A320"}


def _reserve(rid, crew, **over):
    base = {"id": rid, "company_id": "c1", "crew_id": crew, "status": "ACTIVE",
            "called_out": False, "assigned_flight_id": None,
            "response_minutes": 60, "response_status": None, "assignment_id": None,
            "escalated_at": None, "escalation_status": None,
            "called_out_at": None,
            "start_time": "2099-06-01T08:00:00+00:00",
            "end_time": "2099-06-01T18:00:00+00:00", "airport_code": "BGW"}
    base.update(over)
    return base


# ── _standby_state mapping (pure) ────────────────────────────────────────────
def test_standby_state_mapping():
    assert _standby_state(_reserve("s", "c"), NOW) == "AVAILABLE"
    assert _standby_state(_reserve("s", "c", status="EXPIRED"), NOW) == "EXPIRED"
    assert _standby_state(_reserve("s", "c", status="CANCELLED"), NOW) == "CANCELLED"
    assert _standby_state(_reserve("s", "c", called_out=True, status="CALLED_OUT",
                                   called_out_at="2099-06-01T08:55:00+00:00"), NOW) == "CALLED"
    # called out, no response, deadline passed (08:00 + 60m = 09:00 <= NOW 09:00)
    assert _standby_state(_reserve("s", "c", called_out=True, status="CALLED_OUT",
                                   called_out_at="2099-06-01T07:00:00+00:00"), NOW) == "NO_RESPONSE"
    assert _standby_state(_reserve("s", "c", called_out=True, status="ASSIGNED",
                                   response_status="ACCEPTED"), NOW) == "ACCEPTED"
    assert _standby_state(_reserve("s", "c", called_out=True, status="CALLED_OUT",
                                   response_status="REJECTED"), NOW) == "REJECTED"
    assert _standby_state(_reserve("s", "c", called_out=True, status="CALLED_OUT",
                                   response_status="REJECTED",
                                   escalation_status="EXHAUSTED"), NOW) == "EXHAUSTED"


# ── coverage shows the available pool ────────────────────────────────────────
def test_coverage_lists_available_candidates(monkeypatch):
    _patch_engine(monkeypatch)
    store = {"flights": [_flight()], "crew": [{"id": "cr1", "company_id": "c1"}],
             "standby_assignments": [_reserve("s1", "cr1")]}
    res = asyncio.run(standby_coverage("f1", current_user=ADMIN, sb=FakeSb(store)))
    assert res["available_count"] == 1 and res["has_valid_standby"] is True
    assert res["message"] is None
    assert res["candidates"][0]["crew_id"] == "cr1"


# ── coverage shows engaged reserves with states; no valid → message ──────────
def test_coverage_engaged_states_and_no_valid_message(monkeypatch):
    _patch_engine(monkeypatch)
    # All reserves for this flight are non-ACTIVE → no available pool.
    store = {"flights": [_flight()], "crew": [{"id": c, "company_id": "c1"}
                                              for c in ("cr1", "cr2")],
             "standby_assignments": [
                 _reserve("s1", "cr1", status="CALLED_OUT", called_out=True,
                          assigned_flight_id="f1", response_status="REJECTED"),
                 _reserve("s2", "cr2", status="CALLED_OUT", called_out=True,
                          assigned_flight_id="f1",
                          called_out_at="2020-01-01T00:00:00+00:00"),  # timed out (past)
             ]}
    res = asyncio.run(standby_coverage("f1", current_user=ADMIN, sb=FakeSb(store)))
    assert res["available_count"] == 0 and res["has_valid_standby"] is False
    assert res["message"]                               # clear "no reserve" message
    states = {e["crew_id"]: e["state"] for e in res["engaged"]}
    assert states["cr1"] == "REJECTED"
    assert states["cr2"] == "NO_RESPONSE"


# ── rejected/expired/cancelled never appear as available candidates ──────────
def test_invalid_reserves_excluded_from_available(monkeypatch):
    _patch_engine(monkeypatch)
    store = {"flights": [_flight()],
             "crew": [{"id": c, "company_id": "c1"} for c in ("ok", "rej", "exp", "can")],
             "standby_assignments": [
                 _reserve("s_ok", "ok"),                                  # ACTIVE → available
                 _reserve("s_rej", "rej", status="CALLED_OUT", called_out=True,
                          assigned_flight_id="f1", response_status="REJECTED"),
                 _reserve("s_exp", "exp", status="EXPIRED"),
                 _reserve("s_can", "can", status="CANCELLED"),
             ]}
    res = asyncio.run(standby_coverage("f1", current_user=ADMIN, sb=FakeSb(store)))
    avail_crew = {c["crew_id"] for c in res["candidates"]
                  if c.get("compliance_status") not in ("BLOCKED", "RED")}
    assert avail_crew == {"ok"}                          # only the ACTIVE reserve


# ── company scope ────────────────────────────────────────────────────────────
def test_coverage_company_scoped(monkeypatch):
    _patch_engine(monkeypatch)
    store = {"flights": [_flight()],
             "crew": [{"id": "cr1", "company_id": "c1"}, {"id": "cr9", "company_id": "c2"}],
             "standby_assignments": [
                 _reserve("s1", "cr1"),
                 _reserve("s9", "cr9", company_id="c2"),   # other company
             ]}
    res = asyncio.run(standby_coverage("f1", current_user=ADMIN, sb=FakeSb(store)))
    assert {c["crew_id"] for c in res["candidates"]} == {"cr1"}


def test_coverage_does_not_create_assignments(monkeypatch):
    _patch_engine(monkeypatch)
    store = {"flights": [_flight()], "crew": [{"id": "cr1", "company_id": "c1"}],
             "standby_assignments": [_reserve("s1", "cr1")], "assignments": []}
    asyncio.run(standby_coverage("f1", current_user=ADMIN, sb=FakeSb(store)))
    assert store["assignments"] == []                    # READ-ONLY


# ── IROPS reads the standby pool first ───────────────────────────────────────
def test_irops_recovery_uses_standby_pool_first(monkeypatch):
    # Stub the shared ranking so we assert IROPS surfaces it (no full engine run).
    pool = [{"id": "s1", "crew_id": "cr1", "compliance_status": "OK",
             "response_minutes": 30, "blocking_reasons": []}]
    monkeypatch.setattr(standby_mod, "_rank_standby_candidates",
                        lambda sb, cid, flight: pool)
    store = {"flights": [_flight()], "aircraft": [], "crew": [], "assignments": []}
    res = asyncio.run(recovery_options("f1", current_user=ADMIN, sb=FakeSb(store)))
    assert res["standby_options"] == pool
    assert res["has_valid_standby"] is True
    assert "crew_options" in res                          # general fallback still present


def test_irops_no_valid_standby_flag(monkeypatch):
    blocked = [{"id": "s1", "crew_id": "cr1", "compliance_status": "BLOCKED",
                "response_minutes": 30, "blocking_reasons": ["تعارض"]}]
    monkeypatch.setattr(standby_mod, "_rank_standby_candidates",
                        lambda sb, cid, flight: blocked)
    store = {"flights": [_flight()], "aircraft": [], "crew": [], "assignments": []}
    res = asyncio.run(recovery_options("f1", current_user=ADMIN, sb=FakeSb(store)))
    assert res["has_valid_standby"] is False              # only a blocked reserve
    assert res["standby_options"] == blocked              # still surfaced (with reason)
