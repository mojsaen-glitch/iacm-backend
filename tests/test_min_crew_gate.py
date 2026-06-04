"""Under-staffing gate — a flight's roster cannot be finalised below the
minimum complement (captain + cockpit floor + cabin floor). The gate lives at
roster finalisation, NOT at publish (publish only opens a flight for assignment).

Run:  py -m pytest tests/test_min_crew_gate.py -q
"""
from app.api.v1.endpoints.flights import _min_crew_shortfalls


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


def _sb(crew_ids, ranks):
    return FakeSb({
        "assignments": [{"crew_id": c} for c in crew_ids],
        "crew": [{"rank": r} for r in ranks],
    })


_FLIGHT = {"id": "f1", "aircraft_type": "B737"}  # floor: 2 pilots, 3 cabin


def test_empty_roster_reports_all_shortfalls():
    out = _min_crew_shortfalls(_sb([], []), _FLIGHT)
    # No captain, no pilots, no cabin.
    assert len(out) == 3
    assert any("قائد" in s for s in out)


def test_missing_captain_blocks_even_with_two_pilots():
    # 2 pilots (FO + SO) and full cabin, but NO captain.
    sb = _sb(["a", "b", "c", "d", "e"],
             ["first_officer", "second_officer", "purser", "cabin_crew", "cabin_crew"])
    out = _min_crew_shortfalls(sb, _FLIGHT)
    assert len(out) == 1 and "قائد" in out[0]


def test_too_few_cabin_blocks():
    sb = _sb(["a", "b", "c"], ["captain", "first_officer", "purser"])  # cabin 1/3
    out = _min_crew_shortfalls(sb, _FLIGHT)
    assert len(out) == 1 and "المقصورة" in out[0]


def test_complete_min_crew_passes():
    sb = _sb(["a", "b", "c", "d", "e"],
             ["captain", "first_officer", "purser", "cabin_crew", "cabin_crew"])
    assert _min_crew_shortfalls(sb, _FLIGHT) == []
