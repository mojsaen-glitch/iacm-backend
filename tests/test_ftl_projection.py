"""FTL projection — the flight being assigned NOW must count toward the limits
BEFORE the decision (97h current + 5h new = 102h is caught, not allowed).

Run:  py -m pytest tests/test_ftl_projection.py -q
"""
from datetime import datetime, timezone, timedelta

from app.core.compliance_engine import ComplianceEngine, Severity, MAX_MONTHLY_HOURS


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


def _engine_with_existing(monthly_hours: float):
    """One existing assignment earlier this month carrying `monthly_hours`."""
    dep = datetime.now(timezone.utc).replace(day=1, hour=6, minute=0,
                                             second=0, microsecond=0)
    store = {
        "assignments": [{"flight_id": "f_old"}],
        "flights": [{"id": "f_old", "departure_time": dep.isoformat(),
                     "duration_hours": monthly_hours, "status": "scheduled"}],
    }
    return ComplianceEngine(FakeSb(store))


def _new_flight(hours: float):
    dep = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    return dep, dep + timedelta(hours=hours)


def test_projection_blocks_when_new_flight_crosses_limit():
    # 97h existing + 5h new = 102h > 100h monthly → BLOCKING.
    eng = _engine_with_existing(97.0)
    dep, arr = _new_flight(5.0)
    issues = eng._check_flight_hours("c1", {}, projected_segs=[(dep, arr)])
    blocking = [i for i in issues if i.severity == Severity.BLOCKING
                and i.rule == "hours_monthly_exceeded"]
    assert blocking, "projected 102h must trigger a BLOCKING monthly overage"


def test_no_projection_would_not_block_same_case():
    # Same 97h existing, but WITHOUT projecting the new flight → no monthly block
    # (proves the projection is what catches it).
    eng = _engine_with_existing(97.0)
    issues = eng._check_flight_hours("c1", {}, projected_segs=None)
    assert not [i for i in issues if i.rule == "hours_monthly_exceeded"]


def test_projection_within_limit_is_allowed():
    # 80h + 5h = 85h < 100h → no monthly block.
    eng = _engine_with_existing(80.0)
    dep, arr = _new_flight(5.0)
    issues = eng._check_flight_hours("c1", {}, projected_segs=[(dep, arr)])
    assert not [i for i in issues if i.rule == "hours_monthly_exceeded"]
    assert MAX_MONTHLY_HOURS == 100.0
