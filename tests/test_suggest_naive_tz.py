"""Regression: suggest_crew must not 500 when a stored flight time is NAIVE
(no timezone). #3 introduced `a2 <= now` where `now` is tz-aware; a naive
arrival_time made it raise TypeError. _dt now normalises to UTC-aware.

Run:  py -m pytest tests/test_suggest_naive_tz.py -q
"""
import asyncio
from datetime import datetime, timezone, timedelta

from app.api.v1.endpoints.assignments import suggest_crew


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


SCHEDULER = {"id": "u1", "role": "scheduler", "company_id": "co1", "is_superuser": False}


def test_suggest_handles_naive_stored_times():
    now = datetime.now(timezone.utc)
    # Selected flight — tz-aware (with offset).
    f_sel = {
        "id": "f_sel", "company_id": "co1",
        "departure_time": (now + timedelta(hours=2)).isoformat(),
        "arrival_time":   (now + timedelta(hours=3)).isoformat(),
        "origin_code": "BGW", "destination_code": "BSR",
        "aircraft_type": None, "flight_number": "IA-364", "duration_hours": 1.0,
    }
    # Past flight the crew already flew — NAIVE arrival (no tz) → the trigger.
    past_arr = (now - timedelta(hours=5)).replace(tzinfo=None).isoformat()
    past_dep = (now - timedelta(hours=6)).replace(tzinfo=None).isoformat()
    f_past = {
        "id": "f_past", "company_id": "co1",
        "departure_time": past_dep, "arrival_time": past_arr,
        "origin_code": "BGW", "destination_code": "BGW",
        "aircraft_type": None, "status": "scheduled", "duration_hours": 1.0,
    }
    store = {
        "flights": [f_sel, f_past],
        "crew": [{"id": "c1", "status": "active", "rank": "cabin_crew",
                  "full_name_ar": "طاقم", "full_name_en": "Crew",
                  "base": "BGW", "max_monthly_hours": 100}],
        "assignments": [{"crew_id": "c1", "flight_id": "f_past"}],
        "documents": [], "training_records": [], "om_articles": [],
    }
    # Must NOT raise (previously TypeError: naive vs aware → 500).
    res = asyncio.run(suggest_crew("f_sel", current_user=SCHEDULER,
                                   sb=FakeSb(store), limit=12))
    assert "candidates" in res
    assert isinstance(res["candidates"], list)
    # The one crew member is returned and scored.
    assert any(c["crew_id"] == "c1" for c in res["candidates"])
    for c in res["candidates"]:
        assert "assignment_score" in c and "assignment_rank" in c
