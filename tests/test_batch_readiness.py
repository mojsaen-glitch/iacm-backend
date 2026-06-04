"""Phase B — batched roster readiness board.

Proves the board is computed from a few BULK queries (not per-crew N×) and that
each crew gets hours + readiness fields, with a blocked crew forced to BLOCKED.

Run:  py -m pytest tests/test_batch_readiness.py -q
"""
from datetime import datetime, timezone

from app.core.compliance_engine import ComplianceEngine


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        store.setdefault("_query_count", [0])
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def execute(self):
        self.store["_query_count"][0] += 1
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


def _dep_this_month(hours_into=6):
    return datetime.now(timezone.utc).replace(day=1, hour=hours_into, minute=0,
                                              second=0, microsecond=0)


def test_board_returns_fields_and_is_batched():
    dep = _dep_this_month()
    store = {
        "crew": [
            {"id": "c1", "status": "active", "rank": "captain", "max_monthly_hours": 100},
            {"id": "c2", "status": "active", "rank": "purser", "max_monthly_hours": 100},
        ],
        "assignments": [{"crew_id": "c1", "flight_id": "f1"}],
        "flights": [{"id": "f1", "departure_time": dep.isoformat(),
                     "arrival_time": dep.isoformat(), "duration_hours": 20.0,
                     "status": "scheduled"}],
        "documents": [], "training_records": [],
    }
    sb = FakeSb(store)
    board = ComplianceEngine(sb).batch_readiness("co1")
    assert set(board) == {"c1", "c2"}
    c1 = board["c1"]
    for f in ("monthly_flight_hours", "last_28day_hours", "yearly_hours",
              "max_monthly_hours", "readiness_score", "readiness_status",
              "readiness_color", "readiness_reasons", "rest_status"):
        assert f in c1
    assert c1["monthly_flight_hours"] == 20.0
    # Batched: a small constant number of queries, NOT one-per-crew.
    assert store["_query_count"][0] <= 6


def test_blocked_crew_is_blocked_on_board():
    store = {
        "crew": [{"id": "c9", "status": "blocked", "rank": "captain", "max_monthly_hours": 100}],
        "assignments": [], "flights": [], "documents": [], "training_records": [],
    }
    board = ComplianceEngine(FakeSb(store)).batch_readiness("co1")
    assert board["c9"]["readiness_status"] == "BLOCKED"
    assert board["c9"]["readiness_color"] == "red"


def test_clean_crew_is_ready():
    store = {
        "crew": [{"id": "c3", "status": "active", "rank": "first_officer", "max_monthly_hours": 100}],
        "assignments": [], "flights": [], "documents": [], "training_records": [],
    }
    board = ComplianceEngine(FakeSb(store)).batch_readiness("co1")
    assert board["c3"]["readiness_status"] == "READY"
    assert board["c3"]["monthly_flight_hours"] == 0.0
