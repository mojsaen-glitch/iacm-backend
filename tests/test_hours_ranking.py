"""Real-hours consumers — IROPS fairness ranking + pre-assignment projection
must use COMPUTED hours, never the unmaintained crew.monthly_flight_hours.

Run:  py -m pytest tests/test_hours_ranking.py -q
"""
import asyncio

import app.api.v1.endpoints.irops as irops_mod
import app.api.v1.endpoints.assignments as asg_mod
from app.api.v1.endpoints.irops import recovery_options
from app.api.v1.endpoints.assignments import assignment_projection


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.rows = list(store.get(name, []))
    def select(self, *a, **k): return self
    def eq(self, col, val):
        self.rows = [r for r in self.rows if r.get(col) == val]
        return self
    def neq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": list(self.rows)})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


OPS = {"id": "u1", "role": "ops_manager", "company_id": "c1", "is_superuser": False}


# ── IROPS: ranking ignores the stored field ───────────────────────────────────
def _irops_store():
    return {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-1",
                     "departure_time": "2099-01-01T10:00:00+00:00",
                     "arrival_time": "2099-01-01T12:00:00+00:00",
                     "aircraft_type": "A320"}],
        "aircraft": [],
        "assignments": [],
        "crew": [
            # Stored field LIES: A looks free (stored 0) but really flew 50h;
            # B looks loaded (stored 99) but really flew 5h.
            {"id": "A", "company_id": "c1", "status": "active", "rank": "captain",
             "full_name_ar": "أ", "monthly_flight_hours": 0, "total_flight_hours": 0},
            {"id": "B", "company_id": "c1", "status": "active", "rank": "captain",
             "full_name_ar": "ب", "monthly_flight_hours": 99, "total_flight_hours": 0},
        ],
    }


def test_irops_ranks_by_real_hours_not_stored(monkeypatch):
    import app.core.monthly_hours as mh
    monkeypatch.setattr(mh, "month_hours_by_crew",
                        lambda sb, cid, dh_credit=None: {"A": 50.0, "B": 5.0})
    res = asyncio.run(recovery_options("f1", current_user=OPS, sb=FakeSb(_irops_store())))
    order = [c["id"] for c in res["crew_options"]]
    assert order == ["B", "A"]            # real 5h < 50h — stored field ignored
    assert res["crew_options"][0]["computed_month_hours"] == 5.0


def test_irops_falls_back_to_stored_when_batch_fails(monkeypatch):
    import app.core.monthly_hours as mh
    def _boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(mh, "month_hours_by_crew", _boom)
    res = asyncio.run(recovery_options("f1", current_user=OPS, sb=FakeSb(_irops_store())))
    order = [c["id"] for c in res["crew_options"]]
    assert order == ["A", "B"]            # graceful fallback: stored 0 < 99


# ── /assignments/month-hours: the auto-assign fairness feed ──────────────────
def test_month_hours_endpoint_returns_report_policy_map(monkeypatch):
    """ONE batch endpoint wrapping month_hours_by_crew (operating only,
    cancelled excluded, Baghdad month) — no per-crew calls."""
    import app.core.monthly_hours as mh
    from app.api.v1.endpoints.assignments import month_hours
    calls = []
    monkeypatch.setattr(mh, "month_hours_by_crew",
                        lambda sb, cid, dh_credit=None:
                        calls.append(cid) or {"c1": 3.0, "c2": 0.5})
    res = asyncio.run(month_hours(current_user=OPS, sb=FakeSb({})))
    assert res == {"hours": {"c1": 3.0, "c2": 0.5}}
    assert calls == ["c1"]                  # company-scoped, single batch


# ── Projection: hours come from the ENGINE, not crew.monthly_flight_hours ────
class _StubEngine:
    def __init__(self, sb): pass
    def batch_readiness(self, cid, crew_rows=None):
        # The engine's COMPUTED figures (crew row stored field is 0/absent).
        return {"crA": {"monthly_flight_hours": 50.0, "last_28day_hours": 60.0,
                        "yearly_hours": 300.0, "max_monthly_hours": 100}}
    def check_crew(self, **k):
        return {"status": "GREEN", "issues": []}
    def _readiness_from_result(self, res):
        return {"readiness_status": "READY", "readiness_score": 100}


def test_projection_uses_engine_hours(monkeypatch):
    monkeypatch.setattr(asg_mod, "ComplianceEngine", _StubEngine)
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-1",
                     "departure_time": "2099-01-01T10:00:00+00:00",
                     "arrival_time": "2099-01-01T12:00:00+00:00",
                     "duration_hours": 2.0, "origin_code": "BGW",
                     "destination_code": "EBL", "aircraft_type": "A320"}],
        # Stored monthly field deliberately ABSENT/dead on the crew row.
        "crew": [{"id": "crA", "company_id": "c1", "status": "active",
                  "rank": "captain", "max_monthly_hours": 100}],
    }
    res = asyncio.run(assignment_projection("f1", "crA", current_user=OPS,
                                            sb=FakeSb(store)))
    # 50 (engine-computed) + 2 (new flight) — NOT 0 + 2 from the dead field.
    assert res["projected"]["monthly_hours"] == 52.0
    assert res["projected"]["last_28day_hours"] == 62.0
