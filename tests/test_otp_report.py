"""OTP report — pure compute + endpoint (READ-ONLY). No writes/engine/GD.

Run:  py -m pytest tests/test_otp_report.py -q
"""
import asyncio

import pytest

from app.core.otp_report import compute_otp
from app.api.v1.endpoints.otp import otp_report
from app.core.exceptions import ForbiddenError


def _f(dep, arr, atd=None, ata=None, status="arrived", reason=None,
       ac="A320", reg="YI-A1"):
    return {
        "id": dep, "flight_number": "IA-1", "status": status,
        "departure_time": dep, "arrival_time": arr,
        "actual_departure_time": atd, "actual_arrival_time": ata,
        "delay_reason_code": reason, "aircraft_type": ac,
        "aircraft_registration": reg,
    }


# ── Pure compute ──────────────────────────────────────────────────────────────
def test_on_time_within_threshold():
    flights = [
        # +5 min dep, +10 min arr → both on-time (≤15)
        _f("2026-06-01T10:00:00+00:00", "2026-06-01T12:00:00+00:00",
           "2026-06-01T10:05:00+00:00", "2026-06-01T12:10:00+00:00"),
        # +40 min dep (late), +30 arr (late), reason 'weather'
        _f("2026-06-01T14:00:00+00:00", "2026-06-01T16:00:00+00:00",
           "2026-06-01T14:40:00+00:00", "2026-06-01T16:30:00+00:00",
           reason="weather"),
    ]
    r = compute_otp(flights, threshold_min=15)
    assert r["total_flights"] == 2
    assert r["with_atd"] == 2 and r["with_ata"] == 2
    assert r["departure_on_time"] == 1 and r["arrival_on_time"] == 1
    assert r["departure_otp_pct"] == 50.0 and r["arrival_otp_pct"] == 50.0


def test_missing_actual_does_not_break_and_excluded_from_ratio():
    flights = [
        _f("2026-06-01T10:00:00+00:00", "2026-06-01T12:00:00+00:00",
           "2026-06-01T10:05:00+00:00", "2026-06-01T12:05:00+00:00"),  # has actual
        _f("2026-06-02T10:00:00+00:00", "2026-06-02T12:00:00+00:00"),  # NO actual
    ]
    r = compute_otp(flights)
    assert r["total_flights"] == 2
    assert r["with_atd"] == 1 and r["with_ata"] == 1
    assert r["missing_actual"] == 1
    # Ratio is over flights WITH actual only → 1/1 on-time.
    assert r["departure_otp_pct"] == 100.0


def test_average_delay_counts_lateness_only():
    flights = [
        # 10 min early dep (−10) and 60 min late dep (+60) → avg LATE = 60
        _f("2026-06-01T10:00:00+00:00", "2026-06-01T12:00:00+00:00",
           "2026-06-01T09:50:00+00:00", "2026-06-01T12:00:00+00:00"),
        _f("2026-06-02T10:00:00+00:00", "2026-06-02T12:00:00+00:00",
           "2026-06-02T11:00:00+00:00", "2026-06-02T12:50:00+00:00",
           reason="technical"),
    ]
    r = compute_otp(flights)
    assert r["avg_departure_delay_min"] == 60.0     # only the +60 counts
    assert r["avg_arrival_delay_min"] == 50.0       # only the +50 arr


def test_delay_reason_pareto_sorted():
    base = "2026-06-0{}T10:00:00+00:00"
    arr = "2026-06-0{}T12:00:00+00:00"
    late = "2026-06-0{}T11:00:00+00:00"   # +60 dep
    flights = []
    for i, reason in enumerate(
            ["weather", "weather", "weather", "technical", "atc"], start=1):
        flights.append(_f(base.format(i), arr.format(i),
                          late.format(i), arr.format(i), reason=reason))
    r = compute_otp(flights)
    pareto = r["delay_reasons_pareto"]
    assert pareto[0] == {"code": "weather", "count": 3}
    assert {p["code"] for p in pareto} == {"weather", "technical", "atc"}


def test_cancelled_excluded():
    flights = [
        _f("2026-06-01T10:00:00+00:00", "2026-06-01T12:00:00+00:00",
           status="cancelled"),
        _f("2026-06-02T10:00:00+00:00", "2026-06-02T12:00:00+00:00",
           "2026-06-02T10:05:00+00:00", "2026-06-02T12:05:00+00:00"),
    ]
    r = compute_otp(flights)
    assert r["total_flights"] == 1


def test_empty_input_safe():
    r = compute_otp([])
    assert r["total_flights"] == 0
    assert r["departure_otp_pct"] is None
    assert r["delay_reasons_pareto"] == []


# ── Endpoint (filters + role gate + company scope) ───────────────────────────
class _Q:
    def __init__(self, sb, table):
        self.sb, self.table, self._f = sb, table, []
    def select(self, *a, **k): return self
    def eq(self, f, v): self._f.append(("eq", f, v)); return self
    def gte(self, f, v): self._f.append(("gte", f, v)); return self
    def lte(self, f, v): self._f.append(("lte", f, v)); return self
    def order(self, *a, **k): return self
    def execute(self):
        rows = self.sb.store.get(self.table, [])
        for op, f, v in self._f:
            if op == "eq":
                rows = [r for r in rows if r.get(f) == v]
            if op == "gte":
                rows = [r for r in rows if str(r.get(f) or "") >= str(v)]
            if op == "lte":
                rows = [r for r in rows if str(r.get(f) or "") <= str(v)]
        return type("R", (), {"data": list(rows)})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, n): return _Q(self, n)


OPS = {"id": "u1", "role": "ops_manager", "company_id": "c1", "is_superuser": False}
CREW = {"id": "u2", "role": "crew", "company_id": "c1", "is_superuser": False}


def _store():
    return {"flights": [
        {**_f("2026-06-01T10:00:00+00:00", "2026-06-01T12:00:00+00:00",
              "2026-06-01T10:05:00+00:00", "2026-06-01T12:05:00+00:00",
              ac="A320", reg="YI-A1"), "company_id": "c1"},
        {**_f("2026-06-02T10:00:00+00:00", "2026-06-02T12:00:00+00:00",
              "2026-06-02T11:00:00+00:00", "2026-06-02T12:00:00+00:00",
              ac="B737", reg="YI-B2", reason="weather"), "company_id": "c1"},
        {**_f("2026-06-03T10:00:00+00:00", "2026-06-03T12:00:00+00:00",
              "2026-06-03T10:02:00+00:00", "2026-06-03T12:02:00+00:00"),
         "company_id": "c2"},   # other company — must be excluded
    ]}


# Called directly (not via FastAPI) → must pass Query params explicitly, else
# they arrive as FieldInfo objects rather than their defaults.
def _call(user, sb, **kw):
    args = dict(date_from=None, date_to=None, aircraft_type=None,
                registration=None, reason_code=None, threshold_min=15,
                company_id=None)
    args.update(kw)
    return asyncio.run(otp_report(current_user=user, sb=sb, **args))


def test_endpoint_company_scoped():
    res = _call(OPS, FakeSb(_store()))
    assert res["company_id"] == "c1"
    assert res["total_flights"] == 2          # c2 flight excluded


def test_endpoint_aircraft_filter():
    res = _call(OPS, FakeSb(_store()), aircraft_type="A320")
    assert res["total_flights"] == 1


def test_endpoint_reason_filter():
    res = _call(OPS, FakeSb(_store()), reason_code="weather")
    assert res["total_flights"] == 1         # only the B737 weather-delayed leg


def test_endpoint_role_gate():
    with pytest.raises(ForbiddenError):
        _call(CREW, FakeSb(_store()))
