"""Deadhead / Positioning Crew — `duty_type` semantics.

Only `operating` assignments count toward the GenDec complement. The other
duty types (deadhead / standby / observer / training) RIDE the flight but
never satisfy a missing role and never trip the over-staffing cap.

These tests focus on the PURE LOGIC parts that don't need Supabase — the
constants and helpers in assignments.py, and the min-crew shortfalls helper
in flights.py with a stubbed Supabase client.

Run:  venv/Scripts/python -m pytest tests/test_duty_type.py -q
"""
from app.api.v1.endpoints.assignments import _DUTY_TYPES, _OPERATING
from app.api.v1.endpoints import flights as flights_mod


# ── 1. Enum + default ──────────────────────────────────────────────────────
def test_duty_type_enum_matches_spec():
    """The legal values must match the SQL CHECK constraint and the spec."""
    assert _DUTY_TYPES == {
        "operating", "deadhead", "standby", "observer", "training"
    }


def test_operating_is_the_default_constant():
    """The default duty_type (used when legacy rows have no value) is operating
    — old data keeps its previous behaviour after the migration."""
    assert _OPERATING == "operating"


# ── 2. min_crew_shortfalls counts operating only ───────────────────────────
class _StubResp:
    def __init__(self, data): self.data = data


class _StubQuery:
    """Honours `.in_(field, values)` so a crew lookup filtered by id returns
    only the matching rows — matches what Supabase actually does, and makes
    the shortfalls helper's "operating-only" logic observable."""
    def __init__(self, rows):
        self._rows = list(rows)
        self._filter = None        # (field, allowed_set) once .in_() is called
    def select(self, *_args, **_kwargs): return self
    def eq(self, *_args, **_kwargs):     return self
    def in_(self, field, values):
        self._filter = (field, set(values))
        return self
    def execute(self):
        if self._filter is None:
            return _StubResp(self._rows)
        field, allowed = self._filter
        return _StubResp([r for r in self._rows if r.get(field) in allowed])


class _StubSb:
    """Tiny Supabase stand-in: `sb.table('x')` returns a query whose execute()
    yields a preset list. Each record in `crew` should carry its own `id` so
    the .in_('id', ...) filter on the operating crew works as in production."""
    def __init__(self, *, assignments, crew):
        self._assignments = assignments
        self._crew = crew
    def table(self, name):
        if name == "assignments":
            return _StubQuery(self._assignments)
        if name == "crew":
            return _StubQuery(self._crew)
        return _StubQuery([])


def test_min_crew_excludes_deadhead_cabin():
    """3 operating CC + 1 deadhead CC must stay 3 (not 4) — the deadhead
    is a passenger riding the flight, not a working cabin crew member."""
    flight = {"id": "F1", "aircraft_type": "B737"}
    sb = _StubSb(
        assignments=[
            {"crew_id": "c1", "duty_type": "operating"},
            {"crew_id": "c2", "duty_type": "operating"},
            {"crew_id": "c3", "duty_type": "operating"},
            {"crew_id": "c4", "duty_type": "deadhead"},   # passenger
            # plus a captain so the captain-presence check passes
            {"crew_id": "p1", "duty_type": "operating"},
            {"crew_id": "p2", "duty_type": "operating"},
        ],
        crew=[
            {"id": "c1", "rank": "cabin_crew"},
            {"id": "c2", "rank": "cabin_crew"},
            {"id": "c3", "rank": "cabin_crew"},
            {"id": "c4", "rank": "cabin_crew"},          # deadhead — filtered out by .in_()
            {"id": "p1", "rank": "pilot_captain"},
            {"id": "p2", "rank": "pilot_first_officer"},
        ],
    )
    out = flights_mod._min_crew_shortfalls(sb, flight)
    # B737 cabin floor = 3. Operating cabin = 3 → no shortfall on cabin.
    # Pilots = 2 (need 2), captain present → no shortfall on pilots either.
    assert out == [], f"unexpected shortfalls: {out}"


def test_min_crew_blocks_when_operating_below_floor():
    """3 operating + 2 deadhead on a B777 (cabin floor = 8) still blocks
    because only the 3 operating count."""
    flight = {"id": "F2", "aircraft_type": "B777"}
    sb = _StubSb(
        assignments=[
            {"crew_id": "p1", "duty_type": "operating"},
            {"crew_id": "p2", "duty_type": "operating"},
            {"crew_id": "c1", "duty_type": "operating"},
            {"crew_id": "c2", "duty_type": "operating"},
            {"crew_id": "c3", "duty_type": "operating"},
            {"crew_id": "c4", "duty_type": "deadhead"},
            {"crew_id": "c5", "duty_type": "deadhead"},
        ],
        crew=[
            {"id": "p1", "rank": "pilot_captain"},
            {"id": "p2", "rank": "pilot_first_officer"},
            {"id": "c1", "rank": "cabin_crew"},
            {"id": "c2", "rank": "cabin_crew"},
            {"id": "c3", "rank": "cabin_crew"},
            {"id": "c4", "rank": "cabin_crew"},
            {"id": "c5", "rank": "cabin_crew"},
        ],
    )
    out = flights_mod._min_crew_shortfalls(sb, flight)
    assert any("طاقم المقصورة" in m for m in out), \
        f"expected cabin shortfall, got {out}"


def test_legacy_assignments_default_to_operating():
    """A pre-migration row has no duty_type field. The helper must treat it
    as operating (the documented default) — never silently dropping it."""
    flight = {"id": "F3", "aircraft_type": "B737"}
    sb = _StubSb(
        assignments=[
            {"crew_id": "p1"},                        # no duty_type
            {"crew_id": "p2", "duty_type": None},     # explicit null
            {"crew_id": "c1"},
            {"crew_id": "c2"},
            {"crew_id": "c3"},
        ],
        crew=[
            {"id": "p1", "rank": "pilot_captain"},
            {"id": "p2", "rank": "pilot_first_officer"},
            {"id": "c1", "rank": "cabin_crew"},
            {"id": "c2", "rank": "cabin_crew"},
            {"id": "c3", "rank": "cabin_crew"},
        ],
    )
    out = flights_mod._min_crew_shortfalls(sb, flight)
    # All rows treated as operating → captain + 2 pilots + 3 cabin = no shortfall.
    assert out == [], f"unexpected shortfalls: {out}"


def test_no_captain_with_only_deadhead_captain_still_blocks():
    """A deadhead captain doesn't satisfy the captain-presence requirement —
    they're not commanding the aircraft, just riding."""
    flight = {"id": "F4", "aircraft_type": "B737"}
    sb = _StubSb(
        assignments=[
            {"crew_id": "p1", "duty_type": "deadhead"},   # captain riding home
            {"crew_id": "p2", "duty_type": "operating"},  # only F/O working
            {"crew_id": "c1", "duty_type": "operating"},
            {"crew_id": "c2", "duty_type": "operating"},
            {"crew_id": "c3", "duty_type": "operating"},
        ],
        crew=[
            {"id": "p1", "rank": "pilot_captain"},        # deadhead — filtered out
            {"id": "p2", "rank": "pilot_first_officer"},
            {"id": "c1", "rank": "cabin_crew"},
            {"id": "c2", "rank": "cabin_crew"},
            {"id": "c3", "rank": "cabin_crew"},
        ],
    )
    out = flights_mod._min_crew_shortfalls(sb, flight)
    assert any("قائد" in m for m in out), f"expected captain shortfall, got {out}"


def test_all_non_operating_duty_types_excluded():
    """deadhead, standby, observer, training — none satisfy the GenDec."""
    flight = {"id": "F5", "aircraft_type": "B737"}
    sb = _StubSb(
        assignments=[
            {"crew_id": "p1", "duty_type": "operating"},
            {"crew_id": "p2", "duty_type": "operating"},
            {"crew_id": "c1", "duty_type": "deadhead"},
            {"crew_id": "c2", "duty_type": "standby"},
            {"crew_id": "c3", "duty_type": "observer"},
            {"crew_id": "c4", "duty_type": "training"},
        ],
        crew=[
            {"id": "p1", "rank": "pilot_captain"},
            {"id": "p2", "rank": "pilot_first_officer"},
            # 4 cabin crew but ALL non-operating → filtered out by .in_().
            {"id": "c1", "rank": "cabin_crew"},
            {"id": "c2", "rank": "cabin_crew"},
            {"id": "c3", "rank": "cabin_crew"},
            {"id": "c4", "rank": "cabin_crew"},
        ],
    )
    out = flights_mod._min_crew_shortfalls(sb, flight)
    # B737 cabin floor = 3, operating cabin = 0 → shortfall.
    assert any("طاقم المقصورة" in m for m in out), \
        f"expected cabin shortfall (all non-op), got {out}"
