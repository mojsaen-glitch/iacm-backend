"""Pending-acceptance reminders — gentle (≤48h) / urgent (≤6h) tiers,
notification-table dedupe, scheduler follow-up near departure.

Run:  py -m pytest tests/test_acceptance_reminders.py -q
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.api.v1.endpoints.assignments as asg_mod
import app.api.v1.endpoints.flights as fl_mod
from app.api.v1.endpoints.assignments import (
    _scan_company_acceptance_reminders, run_acceptance_reminders)
from app.core.exceptions import ForbiddenError
from app.services import push_service


class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def eq(self, f, v):  self._filters.append(("eq", f, v));  return self
    def neq(self, f, v): self._filters.append(("neq", f, v)); return self
    def gte(self, f, v): self._filters.append(("gte", f, v)); return self
    def lte(self, f, v): self._filters.append(("lte", f, v)); return self
    def in_(self, f, v): self._filters.append(("in", f, set(v))); return self
    def insert(self, p): self._op, self._payload = "insert", p; return self

    def _match(self, row):
        for op, f, v in self._filters:
            x = row.get(f)
            if op == "eq" and x != v: return False
            if op == "neq" and x == v: return False
            if op == "in" and x not in v: return False
            if op == "gte" and not (str(x or "") >= str(v)): return False
            if op == "lte" and not (str(x or "") <= str(v)): return False
        return True

    def execute(self):
        rows = self.sb.store.get(self.table, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            self.sb.store[self.table] = rows
            return _R(items)
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store):
        self.store = {k: [dict(r) for r in v] for k, v in store.items()}

    def table(self, name): return _Q(self, name)

    def notes(self, ntype=None):
        rows = self.store.get("notifications", [])
        return [n for n in rows if ntype is None or n.get("type") == ntype]


def _iso(dt): return dt.isoformat()


def _store(dep_hours_from_now=30.0, publish="published", status="scheduled",
           extra_assignments=None):
    now = datetime.now(timezone.utc)
    dep = now + timedelta(hours=dep_hours_from_now)
    asgs = [
        # pending (no response at all)
        {"id": "a1", "flight_id": "f1", "crew_id": "cP", "duty_type": "operating",
         "acknowledged": False, "declined": False, "admin_confirmed": False},
        # accepted / declined / admin-confirmed — must stay silent
        {"id": "a2", "flight_id": "f1", "crew_id": "cA", "duty_type": "operating",
         "acknowledged": True, "declined": False, "admin_confirmed": False},
        {"id": "a3", "flight_id": "f1", "crew_id": "cD", "duty_type": "operating",
         "acknowledged": False, "declined": True, "admin_confirmed": False},
        {"id": "a4", "flight_id": "f1", "crew_id": "cM", "duty_type": "operating",
         "acknowledged": False, "declined": False, "admin_confirmed": True},
        # pending RIDER (deadhead) — riders never block, never nagged
        {"id": "a5", "flight_id": "f1", "crew_id": "cR", "duty_type": "deadhead",
         "acknowledged": False, "declined": False, "admin_confirmed": False},
    ] + (extra_assignments or [])
    return {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-9",
                     "origin_code": "BGW", "destination_code": "EBL",
                     "departure_time": _iso(dep),
                     "publish_status": publish, "status": status}],
        "assignments": asgs,
        "users": [{"id": f"u_{c}", "crew_id": c, "company_id": "c1",
                   "is_active": True}
                  for c in ("cP", "cA", "cD", "cM", "cR")],
        "crew": [{"id": c, "company_id": "c1", "full_name_ar": f"عضو {c}"}
                 for c in ("cP", "cA", "cD", "cM", "cR")],
        "notifications": [],
    }


@pytest.fixture
def quiet(monkeypatch):
    calls = []

    def _record(sb, cid, roles, ntype, *a, **k):
        # Mimic the REAL helper: it persists notification rows (type +
        # related_flight_id, NO company_id) — the dedupe reads them back.
        calls.append((ntype, a))
        sb.store.setdefault("notifications", []).append({
            "user_id": "u_role", "type": ntype,
            "related_flight_id": a[-1] if a else None,
            "created_at": _iso(datetime.now(timezone.utc)),
        })
        return 1

    monkeypatch.setattr(fl_mod, "_insert_role_notifications", _record)
    monkeypatch.setattr(push_service, "send_to_users", lambda *a, **k: 0)
    return calls


def test_only_pending_get_reminded(quiet):
    sb = FakeSb(_store(dep_hours_from_now=30))
    out = _scan_company_acceptance_reminders(sb, "c1")
    assert out["gentle_sent"] == 1 and out["urgent_sent"] == 0
    notes = sb.notes("assignment_acceptance_reminder")
    assert len(notes) == 1 and notes[0]["user_id"] == "u_cP"
    # accepted / declined / admin-confirmed / rider — all silent
    assert {n["user_id"] for n in sb.notes()} == {"u_cP"}


def test_draft_cancelled_or_departed_flights_skipped(quiet):
    for kw in ({"publish": "draft"}, {"status": "cancelled"},
               {"dep_hours_from_now": -2.0}):
        sb = FakeSb(_store(**kw))
        out = _scan_company_acceptance_reminders(sb, "c1")
        assert out["gentle_sent"] == 0 and out["urgent_sent"] == 0
        assert sb.notes() == []


def test_no_duplicates_within_window(quiet):
    sb = FakeSb(_store(dep_hours_from_now=30))
    _scan_company_acceptance_reminders(sb, "c1")
    out2 = _scan_company_acceptance_reminders(sb, "c1")   # immediate re-run
    assert out2["gentle_sent"] == 0 and out2["deduped"] == 1
    assert len(sb.notes("assignment_acceptance_reminder")) == 1


def test_urgent_tier_and_ops_followup_near_departure(quiet):
    sb = FakeSb(_store(dep_hours_from_now=3))
    out = _scan_company_acceptance_reminders(sb, "c1")
    assert out["urgent_sent"] == 1 and out["gentle_sent"] == 0
    assert len(sb.notes("assignment_acceptance_urgent")) == 1
    # ops/schedulers follow-up fired once, naming the pending member
    assert out["ops_alerts"] == 1
    assert quiet and quiet[0][0] == "acceptance_followup"
    assert "عضو cP" in quiet[0][1][2]

    # re-run: urgent + follow-up both deduped
    out2 = _scan_company_acceptance_reminders(sb, "c1")
    assert out2["urgent_sent"] == 0 and out2["ops_alerts"] == 0


def test_manual_endpoint_role_gate(quiet):
    sb = FakeSb(_store())
    ops = {"id": "u1", "role": "ops_manager", "company_id": "c1",
           "is_superuser": False}
    res = asyncio.run(run_acceptance_reminders(current_user=ops, sb=sb))
    assert res["gentle_sent"] == 1
    crew = {"id": "u2", "role": "crew", "company_id": "c1",
            "is_superuser": False}
    with pytest.raises(ForbiddenError):
        asyncio.run(run_acceptance_reminders(current_user=crew, sb=sb))
