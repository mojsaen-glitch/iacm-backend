"""FDP Monitor — schedule-linked duty snapshot for one crew.

Run:  py -m pytest tests/test_fdp_monitor.py -q
"""
from datetime import datetime, timezone, timedelta

from app.core.compliance_engine import ComplianceEngine, ComplianceStatus


class _Q:
    def __init__(self, store, name): self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


def _two_sector_store(dep_offset_hours: float):
    """A crew with a 2-sector duty starting `dep_offset_hours` from now."""
    base = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=dep_offset_hours)
    f1 = {"id": "f1", "flight_number": "IA-185", "origin_code": "BGW",
          "destination_code": "JED", "status": "scheduled", "aircraft_type": "B737",
          "departure_time": base.isoformat(),
          "arrival_time": (base + timedelta(hours=2)).isoformat()}
    f2 = {"id": "f2", "flight_number": "IA-186", "origin_code": "JED",
          "destination_code": "BGW", "status": "scheduled", "aircraft_type": "B737",
          "departure_time": (base + timedelta(hours=3)).isoformat(),
          "arrival_time": (base + timedelta(hours=5)).isoformat()}
    return {
        "crew": [{"id": "c1", "full_name_ar": "سميرة", "full_name_en": "Samira",
                  "rank": "senior", "status": "active", "max_monthly_hours": 100}],
        "assignments": [{"flight_id": "f1"}, {"flight_id": "f2"}],
        "flights": [f1, f2],
        "documents": [], "training_records": [], "fdp_rules": [],
    }


def test_future_duty_populates_report_sectors_arrival():
    eng = ComplianceEngine(FakeSb(_two_sector_store(dep_offset_hours=24)))
    res = eng.fdp_monitor("c1")
    assert res["sectors"] == 2
    assert len(res["flights"]) == 2
    assert res["report_time_utc"] is not None
    assert res["final_arrival_utc"] is not None
    # Duty is in the future → no FDP used yet, full remaining.
    assert res["fdp_used_minutes"] == 0
    assert res["fdp_remaining_minutes"] == res["fdp_max_minutes"]
    assert res["status"] == ComplianceStatus.GREEN
    assert res["previous_rest_minutes"] is None  # no prior duty


def test_no_flights_returns_note():
    store = _two_sector_store(24)
    store["assignments"] = []
    store["flights"] = []
    res = ComplianceEngine(FakeSb(store)).fdp_monitor("c1")
    assert res["flights"] == []
    assert res.get("note") == "no_flights"


def test_unknown_crew():
    res = ComplianceEngine(FakeSb({"crew": []})).fdp_monitor("zzz")
    assert res["status"] == "UNKNOWN"
