"""
Tests for ComplianceEngine.crew_legality (live OCC legality countdown) and a
conflict case via check_crew. Uses an in-memory fake Supabase client so the
tests run with no network / no real database.

Run:  py -m pytest tests/test_crew_legality.py -q
"""
from datetime import datetime, timezone

from app.core.compliance_engine import ComplianceEngine, ComplianceStatus


# ── In-memory fake of the supabase-py query builder ────────────────────────
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self

    def execute(self):
        return _Resp(list(self._rows))


class FakeSb:
    """Seed with {table_name: [row, ...]}."""
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


REF = datetime(2026, 5, 23, 17, 0, tzinfo=timezone.utc)


def _crew(**over):
    base = {"id": "c1", "company_id": "co1", "status": "active",
            "full_name_ar": "طيار", "full_name_en": "Pilot",
            "max_monthly_hours": 100.0}
    base.update(over)
    return base


def _flight(fid, dep, arr, **over):
    base = {"id": fid, "flight_number": "IA" + fid, "status": "scheduled",
            "departure_time": dep, "arrival_time": arr,
            "origin_code": "BGW", "destination_code": "BSR",
            "duration_hours": None}
    base.update(over)
    return base


# ── 1. Fully legal crew — no duties, no expiries → GREEN ───────────────────
def test_legal_crew_is_green():
    sb = FakeSb({"crew": [_crew()]})
    res = ComplianceEngine(sb).crew_legality("c1", reference_time=REF)
    assert res["status"] == ComplianceStatus.GREEN
    assert res["on_duty"] is False
    assert res["blocking_reasons"] == []
    assert res["remaining_flight_minutes"] > 0
    # No duty history → legal to report now.
    assert res["next_legal_report_time_utc"] == REF.isoformat()


# ── 2. On duty, near the FDP ceiling → warning + YELLOW ────────────────────
def test_near_fdp_limit_warns():
    flt = _flight("F1", "2026-05-23T09:00:00Z", "2026-05-23T18:00:00Z")
    sb = FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "F1"}],
        "flights": [flt],
        # Broad band, 10h cap so reference (17:00) sits 60 min from the ceiling.
        "fdp_rules": [{
            "acclimatisation_state": "acclimated",
            "start_band_from": "00:00:00", "start_band_to": "23:59:59",
            "sectors_from": 1, "sectors_to": 4,
            "max_fdp_minutes": 600, "is_frm": False,
        }],
    })
    res = ComplianceEngine(sb).crew_legality("c1", reference_time=REF)
    assert res["on_duty"] is True
    assert res["remaining_fdp_minutes"] == 60   # 600 cap − 540 elapsed
    assert res["status"] == ComplianceStatus.YELLOW
    assert any("FDP" in w for w in res["warnings"])
    assert res["legal_until_utc"] == "2026-05-23T18:00:00+00:00"


# ── 3. Just landed, rest not yet complete → warning, future report time ────
def test_rest_not_complete_sets_future_report():
    flt = _flight("F2", "2026-05-23T06:00:00Z", "2026-05-23T09:30:00Z")
    sb = FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "F2"}],
        "flights": [flt],
    })
    ref = datetime(2026, 5, 23, 10, 30, tzinfo=timezone.utc)
    res = ComplianceEngine(sb).crew_legality("c1", reference_time=ref)
    assert res["on_duty"] is False
    report = datetime.fromisoformat(res["next_legal_report_time_utc"])
    assert report > ref                       # still resting
    assert res["minimum_rest_required_minutes"] == 10 * 60   # domestic
    assert any("راحة" in w for w in res["warnings"])
    assert res["status"] == ComplianceStatus.YELLOW


# ── 4. Expired document → BLOCKED ──────────────────────────────────────────
def test_expired_document_blocks():
    sb = FakeSb({
        "crew": [_crew()],
        "documents": [{"crew_id": "c1", "document_type": "medical",
                       "expiry_date": "2020-01-01"}],
    })
    res = ComplianceEngine(sb).crew_legality("c1", reference_time=REF)
    assert res["status"] == ComplianceStatus.BLOCKED
    assert res["blocking_reasons"]            # at least one reason


# ── 5. Time conflict (overlapping assignment) → BLOCKED via check_crew ─────
def test_time_conflict_blocks():
    existing = _flight("A", "2026-05-23T08:00:00Z", "2026-05-23T12:00:00Z")
    sb = FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "A"}],
        "flights": [existing],
    })
    res = ComplianceEngine(sb).check_crew(
        crew_id="c1",
        flight_id="NEW",
        flight_departure=datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc),
        flight_arrival=datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc),
        is_international=False,
    )
    assert res["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"] == "assignment_conflict" for i in res["issues"])


# ── 6. Blocked crew status → BLOCKED ───────────────────────────────────────
def test_blocked_crew_status():
    sb = FakeSb({"crew": [_crew(status="blocked")]})
    res = ComplianceEngine(sb).crew_legality("c1", reference_time=REF)
    assert res["status"] == ComplianceStatus.BLOCKED
    assert res["blocking_reasons"]


# ── 7. Unknown crew → graceful error, not an exception ─────────────────────
def test_unknown_crew():
    sb = FakeSb({"crew": []})
    res = ComplianceEngine(sb).crew_legality("ghost", reference_time=REF)
    assert res.get("status") == "UNKNOWN"
    assert "error" in res


# ── 8. Same-day rotation turnaround → NOT treated as rest (no block) ───────
# IA-101 BGW→JED lands 02:50; IA-102 JED→BGW departs 03:50 (1h sit, same
# station). This is one duty's turnaround, not inter-duty rest, so the
# minimum-rest rule must stay silent.
def test_turnaround_same_station_not_rest():
    prev = _flight("OUT", "2026-05-26T01:00:00Z", "2026-05-26T02:50:00Z",
                   origin_code="BGW", destination_code="JED")
    new = _flight("RET", "2026-05-26T03:50:00Z", "2026-05-26T05:05:00Z",
                  origin_code="JED", destination_code="BGW")
    sb = FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "OUT"}],
        "flights": [prev, new],
    })
    issues = ComplianceEngine(sb)._check_rest(
        "c1", datetime(2026, 5, 26, 3, 50, tzinfo=timezone.utc),
        is_international=False, flight_id="RET")
    assert not any(i.rule == "rest_insufficient" for i in issues)


# ── 9. Short gap but DIFFERENT station → genuine insufficient rest (block) ─
# Crew landed at CAI, next sector departs JED 1h later — they aren't physically
# connected, so this is not a turnaround and the rest rule must still block.
def test_short_gap_different_station_still_blocks():
    prev = _flight("A", "2026-05-26T01:00:00Z", "2026-05-26T02:50:00Z",
                   origin_code="BGW", destination_code="CAI")
    new = _flight("B", "2026-05-26T03:50:00Z", "2026-05-26T05:05:00Z",
                  origin_code="JED", destination_code="BGW")
    sb = FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "A"}],
        "flights": [prev, new],
    })
    issues = ComplianceEngine(sb)._check_rest(
        "c1", datetime(2026, 5, 26, 3, 50, tzinfo=timezone.utc),
        is_international=False, flight_id="B")
    assert any(i.rule == "rest_insufficient" for i in issues)


# ── 10. Same station but LONG sit (> turnaround ceiling) → rest applies ────
# A 5h ground stop exceeds MAX_TURNAROUND_HOURS (3h), so it's no longer a
# turnaround — it must satisfy minimum rest and is blocked (5h < 10h domestic).
def test_same_station_long_sit_requires_rest():
    prev = _flight("A", "2026-05-26T00:00:00Z", "2026-05-26T02:00:00Z",
                   origin_code="BGW", destination_code="JED")
    new = _flight("B", "2026-05-26T07:00:00Z", "2026-05-26T09:00:00Z",
                  origin_code="JED", destination_code="BGW")
    sb = FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "A"}],
        "flights": [prev, new],
    })
    issues = ComplianceEngine(sb)._check_rest(
        "c1", datetime(2026, 5, 26, 7, 0, tzinfo=timezone.utc),
        is_international=False, flight_id="B")
    assert any(i.rule == "rest_insufficient" for i in issues)


# ── 11. New duty with proper rest (≥ MIN_REST) → no rest issue ─────────────
def test_new_duty_with_proper_rest_clears():
    prev = _flight("A", "2026-05-25T10:00:00Z", "2026-05-25T13:00:00Z",
                   origin_code="BGW", destination_code="CAI")
    new = _flight("B", "2026-05-26T05:00:00Z", "2026-05-26T08:00:00Z",
                  origin_code="BGW", destination_code="DXB")
    sb = FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "A"}],
        "flights": [prev, new],
    })
    issues = ComplianceEngine(sb)._check_rest(
        "c1", datetime(2026, 5, 26, 5, 0, tzinfo=timezone.utc),  # 16h after landing
        is_international=False, flight_id="B")
    assert not any(i.rule in ("rest_insufficient", "rest_near_limit") for i in issues)


# ── 12. International rest threshold = domestic + 2h ───────────────────────
# An 11h gap (different station ⇒ real rest) is legal domestically (≥10h) but
# short internationally (<12h) → blocks only on the international leg.
def test_international_rest_is_stricter():
    prev = _flight("A", "2026-05-25T20:00:00Z", "2026-05-25T22:00:00Z",
                   origin_code="BGW", destination_code="CAI")
    new = _flight("B", "2026-05-26T09:00:00Z", "2026-05-26T11:00:00Z",
                  origin_code="BGW", destination_code="LHR")
    tables = {
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "A"}],
        "flights": [prev, new],
    }
    next_dep = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)  # 11h after landing
    dom = ComplianceEngine(FakeSb(tables))._check_rest(
        "c1", next_dep, is_international=False, flight_id="B")
    intl = ComplianceEngine(FakeSb(tables))._check_rest(
        "c1", next_dep, is_international=True, flight_id="B")
    assert not any(i.rule == "rest_insufficient" for i in dom)   # 11h ≥ 10h domestic
    assert any(i.rule == "rest_insufficient" for i in intl)      # 11h < 12h international


# ── 13. Expired training → BLOCKED via check_crew ──────────────────────────
def test_expired_training_blocks():
    sb = FakeSb({
        "crew": [_crew()],
        "training_records": [{"crew_id": "c1", "training_type": "recurrent",
                              "expiry_date": "2020-01-01"}],
    })
    res = ComplianceEngine(sb).check_crew(crew_id="c1")
    assert res["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"].startswith("training_expired") for i in res["issues"])


# ── 14. Aircraft type-rating gate (block unqualified, allow qualified) ─────
def test_aircraft_qualification_gate():
    blocked = ComplianceEngine(FakeSb({"crew": [_crew(aircraft_qualifications="B737")]})) \
        .check_crew(crew_id="c1", flight_aircraft_type="B787")
    assert blocked["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"] == "aircraft_not_type_rated" for i in blocked["issues"])

    ok = ComplianceEngine(FakeSb({"crew": [_crew(aircraft_qualifications="B737")]})) \
        .check_crew(crew_id="c1", flight_aircraft_type="B737")
    assert not any(i["rule"] == "aircraft_not_type_rated" for i in ok["issues"])


# ── 15. Turnaround classifier — unit boundaries ────────────────────────────
def test_is_turnaround_boundaries():
    f = ComplianceEngine._is_turnaround
    assert f("JED", "JED", 1.0) is True      # short same-station sit
    assert f("JED", "JED", 3.0) is True      # exactly at the ceiling
    assert f("JED", "JED", 3.01) is False    # over the ceiling → rest applies
    assert f("CAI", "JED", 1.0) is False     # different station → not connected
    assert f(None, "JED", 1.0) is False      # missing station data → not a turnaround


# ── OM binding layer ───────────────────────────────────────────────────────
# A short-gap, different-station flight that the rest rule blocks, used as the
# fixture for OM governance: by default it's BLOCKED; an OM clause can disable,
# downgrade, or stamp it.
def _rest_blocked_sb(om_rows):
    prev = _flight("A", "2026-05-26T01:00:00Z", "2026-05-26T02:50:00Z",
                   origin_code="BGW", destination_code="CAI")
    new = _flight("B", "2026-05-26T03:50:00Z", "2026-05-26T05:05:00Z",
                  origin_code="JED", destination_code="BGW")
    return FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "A"}],
        "flights": [prev, new],
        "om_articles": om_rows,
    })


def _om(**over):
    base = {"id": "OM-C 9.1", "bound_check_key": "rest", "rule_type": "blocking",
            "is_active": True, "affects_compliance": True}
    base.update(over)
    return base


# 16. No OM rows → engine behaves exactly as before (still blocks).
def test_om_absent_keeps_default_block():
    res = ComplianceEngine(_rest_blocked_sb([])).check_crew(
        crew_id="c1", flight_id="B",
        flight_departure=datetime(2026, 5, 26, 3, 50, tzinfo=timezone.utc),
        flight_arrival=datetime(2026, 5, 26, 5, 5, tzinfo=timezone.utc))
    assert res["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"] == "rest_insufficient" for i in res["issues"])


# 17. Bound clause DISABLED → the family stops firing (issue dropped).
def test_om_disabled_rule_drops_issue():
    res = ComplianceEngine(_rest_blocked_sb([_om(is_active=False)])).check_crew(
        crew_id="c1", flight_id="B",
        flight_departure=datetime(2026, 5, 26, 3, 50, tzinfo=timezone.utc),
        flight_arrival=datetime(2026, 5, 26, 5, 5, tzinfo=timezone.utc))
    assert not any(i["rule"] == "rest_insufficient" for i in res["issues"])


# 18. Bound clause = WARNING → block downgraded to advisory (assignable).
def test_om_warning_downgrades_block():
    res = ComplianceEngine(_rest_blocked_sb([_om(rule_type="warning")])).check_crew(
        crew_id="c1", flight_id="B",
        flight_departure=datetime(2026, 5, 26, 3, 50, tzinfo=timezone.utc),
        flight_arrival=datetime(2026, 5, 26, 5, 5, tzinfo=timezone.utc))
    rest = [i for i in res["issues"] if i["rule"] == "rest_insufficient"]
    assert rest and rest[0]["severity"] == "WARNING"
    assert res["status"] != ComplianceStatus.BLOCKED


# 19. Clause number is stamped onto the violation message + om_ref.
def test_om_stamps_clause_number():
    res = ComplianceEngine(_rest_blocked_sb([_om()])).check_crew(
        crew_id="c1", flight_id="B",
        flight_departure=datetime(2026, 5, 26, 3, 50, tzinfo=timezone.utc),
        flight_arrival=datetime(2026, 5, 26, 5, 5, tzinfo=timezone.utc))
    rest = [i for i in res["issues"] if i["rule"] == "rest_insufficient"][0]
    assert rest["om_ref"] == "OM-C 9.1"
    assert rest["message_ar"].startswith("OM-C 9.1:")
    assert res["status"] == ComplianceStatus.BLOCKED


# 20. approval_required → still blocks but flags requires_approval.
def test_om_approval_required_flags():
    res = ComplianceEngine(_rest_blocked_sb([_om(rule_type="approval_required")])).check_crew(
        crew_id="c1", flight_id="B",
        flight_departure=datetime(2026, 5, 26, 3, 50, tzinfo=timezone.utc),
        flight_arrival=datetime(2026, 5, 26, 5, 5, tzinfo=timezone.utc))
    rest = [i for i in res["issues"] if i["rule"] == "rest_insufficient"][0]
    assert rest["severity"] == "BLOCKING"
    assert rest["detail"].get("requires_approval") is True
