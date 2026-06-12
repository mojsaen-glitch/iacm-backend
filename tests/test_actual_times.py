"""ATD/ATA — Phase 1: OCC-only recording, mandatory reason on edits, full
audit, and the reports' explicit Actual→Scheduled fallback.

Run:  py -m pytest tests/test_actual_times.py -q
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.occ import record_actual_times
from app.core.exceptions import ForbiddenError
from app.core.monthly_hours import _leg_hours, crew_flight_hours, month_hours_by_crew


class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, f, v):  self._filters.append(("eq", f, v));  return self
    def neq(self, f, v): self._filters.append(("neq", f, v)); return self
    def gte(self, f, v): self._filters.append(("gte", f, v)); return self
    def lte(self, f, v): self._filters.append(("lte", f, v)); return self
    def lt(self, f, v):  self._filters.append(("lt", f, v));  return self
    def in_(self, f, v): self._filters.append(("in", f, set(v))); return self
    def range(self, a, b): return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self

    @staticmethod
    def _get(row, field):
        cur = row
        for part in str(field).split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    def _match(self, row):
        for op, f, v in self._filters:
            x = self._get(row, f)
            if op == "eq" and x != v: return False
            if op == "neq" and x == v: return False
            if op == "in" and x not in v: return False
            if op == "gte" and not (str(x or "") >= str(v)): return False
            if op == "lte" and not (str(x or "") <= str(v)): return False
            if op == "lt" and not (str(x or "") < str(v)): return False
        return True

    def execute(self):
        rows = self.sb.store.get(self.table, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            self.sb.store[self.table] = rows
            return _R(items)
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            return _R([dict(r) for r in hit])
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store):
        self.store = {k: [dict(r) for r in v] for k, v in store.items()}

    def table(self, name): return _Q(self, name)

    def audits(self, action=None):
        return [r for r in self.store.get("audit_log", [])
                if action is None or r.get("action") == action]


OCC = {"id": "u1", "role": "flight_operations", "company_id": "c1",
       "name_ar": "العمليات", "is_superuser": False}
CREW = {"id": "u2", "role": "crew", "company_id": "c1", "is_superuser": False}


def _flight(**over):
    f = {"id": "f1", "company_id": "c1", "flight_number": "IA-5",
         "departure_time": "2026-06-12T10:00:00+00:00",       # STD
         "arrival_time": "2026-06-12T12:00:00+00:00",         # STA
         "estimated_departure_time": "2026-06-12T10:30:00+00:00",  # ETD
         "duration_hours": 2.0, "status": "departed",
         "actual_departure_time": None, "actual_arrival_time": None}
    f.update(over)
    return f


def _run(sb, body, user=OCC):
    return asyncio.run(record_actual_times("f1", body, current_user=user, sb=sb))


# ── Recording ─────────────────────────────────────────────────────────────────
def test_non_occ_role_forbidden():
    with pytest.raises(ForbiddenError):
        _run(FakeSb({"flights": [_flight()]}), {"atd": "2026-06-12T10:31:00Z"},
             user=CREW)


def test_record_atd_keeps_std_and_etd():
    sb = FakeSb({"flights": [_flight()], "audit_log": []})
    res = _run(sb, {"atd": "2026-06-12T10:31:00Z"})
    f = sb.store["flights"][0]
    assert f["actual_departure_time"].startswith("2026-06-12T10:31")
    assert f["departure_time"] == "2026-06-12T10:00:00+00:00"            # STD ثابت
    assert f["estimated_departure_time"] == "2026-06-12T10:30:00+00:00"  # ETD ثابت
    assert res["edited"] is False
    a = sb.audits("actual_times_recorded")
    assert len(a) == 1 and a[0]["company_id"] == "c1"


def test_record_ata_keeps_sta_and_computes_block():
    sb = FakeSb({"flights": [_flight(actual_departure_time="2026-06-12T10:30:00+00:00")],
                 "audit_log": []})
    res = _run(sb, {"ata": "2026-06-12T12:45:00Z"})
    f = sb.store["flights"][0]
    assert f["arrival_time"] == "2026-06-12T12:00:00+00:00"              # STA ثابت
    assert res["actual_block_hours"] == 2.25                             # 10:30→12:45


def test_ata_before_atd_rejected():
    sb = FakeSb({"flights": [_flight(actual_departure_time="2026-06-12T10:30:00+00:00")]})
    with pytest.raises(HTTPException) as e:
        _run(sb, {"ata": "2026-06-12T10:00:00Z"})
    assert e.value.status_code == 422


# ── Editing requires a reason + full audit ───────────────────────────────────
def test_edit_requires_reason_and_audits_before_after():
    sb = FakeSb({"flights": [_flight(actual_departure_time="2026-06-12T10:30:00+00:00")],
                 "audit_log": []})
    with pytest.raises(HTTPException) as e:
        _run(sb, {"atd": "2026-06-12T10:40:00Z"})        # no reason
    assert e.value.status_code == 422

    res = _run(sb, {"atd": "2026-06-12T10:40:00Z", "reason": "تصحيح توقيت البرج"})
    assert res["edited"] is True
    a = sb.audits("actual_times_updated")[0]
    before = json.loads(a["before_data"]); after = json.loads(a["after_data"])
    assert before["atd"] == "2026-06-12T10:30:00+00:00"
    assert after["atd"].startswith("2026-06-12T10:40")
    assert after["reason"] == "تصحيح توقيت البرج"


def test_migration_guard_when_columns_missing():
    f = _flight(); f.pop("actual_departure_time"); f.pop("actual_arrival_time")
    with pytest.raises(HTTPException) as e:
        _run(FakeSb({"flights": [f]}), {"atd": "2026-06-12T10:31:00Z"})
    assert "ترحيل" in str(e.value.detail)


# ── Reports: actual when present, scheduled fallback ─────────────────────────
def test_leg_hours_actual_vs_fallback():
    assert _leg_hours({"duration_hours": 2.0,
                       "actual_departure_time": "2026-06-12T10:30:00+00:00",
                       "actual_arrival_time": "2026-06-12T13:00:00+00:00"}) == (2.5, True)
    assert _leg_hours({"duration_hours": 2.0}) == (2.0, False)           # old rows
    assert _leg_hours({"duration_hours": 2.0,
                       "actual_departure_time": "2026-06-12T10:30:00+00:00",
                       "actual_arrival_time": None}) == (2.0, False)     # ATD only


def test_crew_profile_uses_actual_then_falls_back():
    store = {
        "assignments": [
            {"flight_id": "fa", "crew_id": "c1", "duty_type": "operating"},
            {"flight_id": "fs", "crew_id": "c1", "duty_type": "operating"},
        ],
        "flights": [
            {"id": "fa", "company_id": "co1", "duration_hours": 2.0,
             "departure_time": "2026-06-10T08:00:00+00:00",
             "actual_departure_time": "2026-06-10T08:10:00+00:00",
             "actual_arrival_time": "2026-06-10T10:40:00+00:00"},        # actual 2.5
            {"id": "fs", "company_id": "co1", "duration_hours": 3.0,
             "departure_time": "2026-06-11T08:00:00+00:00"},             # scheduled 3.0
        ],
    }
    res = crew_flight_hours(FakeSb(store), "co1", "c1")
    assert res["total"] == 5.5                                           # 2.5 + 3.0


def test_month_hours_batch_uses_actual():
    from datetime import datetime, timezone, timedelta
    dep = (datetime.now(timezone.utc) + timedelta(hours=-1))
    iso = dep.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    store = {"assignments": [
        {"crew_id": "c1", "duty_type": "operating",
         "flights": {"duration_hours": 2.0, "departure_time": iso,
                     "status": "arrived", "company_id": "co1",
                     "actual_departure_time": iso,
                     "actual_arrival_time":
                         (dep + timedelta(hours=2, minutes=30))
                         .strftime("%Y-%m-%dT%H:%M:%S+00:00")}},
    ]}
    out = month_hours_by_crew(FakeSb(store), "co1")
    assert out.get("c1") == 2.5                                          # not 2.0
