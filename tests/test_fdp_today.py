"""Roster-wide FDP board (fdp_monitor_today) — every crew scheduled today.

Run:  py -m pytest tests/test_fdp_today.py -q
"""
from datetime import datetime, timezone, timedelta

from app.core.compliance_engine import ComplianceEngine, ComplianceStatus


class _Q:
    """Filtering fake (honours eq / in_ / neq)."""
    def __init__(self, store, name): self.rows = list(store.get(name, []))
    def select(self, *a, **k): return self
    def eq(self, c, v): self.rows = [r for r in self.rows if r.get(c) == v]; return self
    def in_(self, c, vs):
        s = set(vs); self.rows = [r for r in self.rows if r.get(c) in s]; return self
    def neq(self, c, v): self.rows = [r for r in self.rows if r.get(c) != v]; return self
    def order(self, *a, **k): return self
    def range(self, *a, **k): return self
    def execute(self): return type("R", (), {"data": list(self.rows)})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


def _store_one_today():
    dep = datetime.now(timezone.utc).replace(microsecond=0)
    f1 = {"id": "f1", "company_id": "co1", "flight_number": "IA-185",
          "origin_code": "BGW", "destination_code": "NJF", "status": "scheduled",
          "aircraft_type": "B737", "duration_hours": 2.0,
          "departure_time": dep.isoformat(),
          "arrival_time": (dep + timedelta(hours=2)).isoformat()}
    return {
        "flights": [f1],
        "assignments": [{"crew_id": "c1", "flight_id": "f1"}],
        "crew": [{"id": "c1", "company_id": "co1", "full_name_ar": "سميرة",
                  "full_name_en": "Samira", "rank": "senior", "status": "active",
                  "max_monthly_hours": 100}],
        "documents": [], "training_records": [], "fdp_rules": [],
    }


def test_today_board_lists_scheduled_crew():
    board = ComplianceEngine(FakeSb(_store_one_today())).fdp_monitor_today("co1")
    assert len(board) == 1
    row = board[0]
    assert row["crew_id"] == "c1"
    assert row["sectors"] == 1
    assert row["status"] == ComplianceStatus.GREEN
    assert "fdp_remaining_minutes" in row and "previous_rest_minutes" in row


def test_today_board_empty_when_no_flights_today():
    store = _store_one_today()
    # Push the only flight far into the past → not "today".
    old = (datetime.now(timezone.utc) - timedelta(days=10))
    store["flights"][0]["departure_time"] = old.isoformat()
    store["flights"][0]["arrival_time"] = (old + timedelta(hours=2)).isoformat()
    board = ComplianceEngine(FakeSb(store)).fdp_monitor_today("co1")
    assert board == []
