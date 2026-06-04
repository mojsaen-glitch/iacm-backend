"""Crew-complement caps — the rule that a flight takes only its required
complement per position (pilots / cabin / engineer), never more.

Run:  py -m pytest tests/test_fleet_complement.py -q
"""
from app.core.fleet_complement import (
    category_for_rank, required_for_category,
    min_required_for_category, is_captain_rank,
    operational_complement_for, operational_expected_by_role,
    flight_deck_expected_by_role, cabin_crew_expected_by_role,
)


def test_rank_categories():
    assert category_for_rank("captain") == "pilot"
    assert category_for_rank("FIRST_OFFICER") == "pilot"
    assert category_for_rank("second_officer") == "pilot"
    # Engineer is now maintenance (AME) → technical/operational, NOT counted.
    assert category_for_rank("flight_engineer") == "other"
    assert category_for_rank("purser") == "cabin"
    assert category_for_rank("cabin_crew") == "cabin"
    assert category_for_rank("dispatcher") == "other"
    assert category_for_rank(None) == "other"


def test_narrowbody_always_two_pilots():
    # B737 is narrow-body — 2 pilots regardless of block time.
    assert required_for_category("B737", "pilot", 1.0) == 2
    assert required_for_category("B737", "pilot", 12.0) == 2
    assert required_for_category("A320", "pilot", 9.0) == 2


def test_widebody_augments_on_longhaul():
    # Wide-body: 2 pilots short-haul, 4 when block time ≥ 8h.
    assert required_for_category("B787", "pilot", 2.0) == 2
    assert required_for_category("B787", "pilot", 8.0) == 4
    assert required_for_category("B777", "pilot", 9.5) == 4


def test_cabin_uses_ceiling():
    # B737-800 ceiling bumped 4→5 to fit GenDec template (1 SCC + 4 CC).
    assert required_for_category("B737", "cabin", 1.0) == 5
    assert required_for_category("A321", "cabin", 1.0) == 5
    assert required_for_category("B777", "cabin", 1.0) == 12


def test_engineer_zero_on_modern_fleet():
    assert required_for_category("B737", "engineer", 1.0) == 0
    assert required_for_category("A330", "engineer", 1.0) == 0


def test_unknown_type_falls_to_generic():
    assert required_for_category("ZZZ", "pilot", 1.0) == 2
    assert required_for_category(None, "cabin", 1.0) == 4
    assert required_for_category("", "pilot", 12.0) == 2


def test_other_category_never_capped():
    assert required_for_category("B737", "other", 1.0) is None


# ── Minimum-crew floor (under-staffing gate at publish) ──────────────
def test_min_crew_floor_per_type():
    # B737 floor: 2 pilots, 3 cabin (exit count), 0 engineer.
    assert min_required_for_category("B737", "pilot") == 2
    assert min_required_for_category("B737", "cabin") == 3
    assert min_required_for_category("B737", "engineer") == 0
    # Wide-body floors are higher on cabin.
    assert min_required_for_category("B777", "cabin") == 8
    # Unknown type → narrow-body baseline floor.
    assert min_required_for_category("ZZZ", "cabin") == 3
    assert min_required_for_category(None, "pilot") == 2


def test_captain_rank_detection():
    assert is_captain_rank("captain") is True
    assert is_captain_rank("CPT") is True
    assert is_captain_rank("first_officer") is False
    assert is_captain_rank("purser") is False
    assert is_captain_rank(None) is False


# ── Operational (advisory) complement template — GenDec per aircraft type ───
def test_b737_operational_matches_gendec():
    """IAW 911/912 GenDec: 1 AME + 1 L/SH + 3 IFSO (other operational = 0)."""
    op = operational_complement_for("B737")
    assert op == {"ame": 1, "lsh": 1, "ifso": 3, "obs": 0, "us": 0, "tech": 0}


def test_all_known_aircraft_types_have_operational_template():
    for t in ("B737", "A320", "A321", "B787", "B777", "A330", "B747", "A380", "CR9"):
        op = operational_complement_for(t)
        # Every type always carries at least 1 AME + 1 L/SH.
        assert op["ame"] >= 1, t
        assert op["lsh"] >= 1, t
        # IFSO 1–3 depending on aircraft size.
        assert 1 <= op["ifso"] <= 3, t


def test_unknown_type_falls_to_generic_operational():
    op = operational_complement_for("ZZZ")
    assert op == {"ame": 1, "lsh": 1, "ifso": 1, "obs": 0, "us": 0, "tech": 0}
    op2 = operational_complement_for(None)
    assert op2 == op


def test_operational_returned_dicts_are_copies():
    """Callers may mutate the result without poisoning the registry."""
    a = operational_complement_for("B737")
    a["ame"] = 99
    b = operational_complement_for("B737")
    assert b["ame"] == 1, "registry was mutated by caller"


def test_operational_by_role_uses_canonical_role_keys():
    by_role = operational_expected_by_role("B737")
    # Maps to crew_roles registry keys (not short codes).
    assert by_role["aircraft_maintenance_engineer"] == 1
    assert by_role["load_sheet_officer"] == 1
    assert by_role["in_flight_security_officer"] == 3
    assert by_role["observer"] == 0
    assert by_role["security_staff"] == 0
    assert by_role["technical_staff"] == 0


def test_b737_cabin_max_bumped_for_gendec():
    """SCC + 4 CC = 5 cabin crew must fit under the B737 ceiling."""
    assert required_for_category("B737", "cabin", 1.0) == 5
    # Safety floor for B737 cabin remains the 3-exit-count baseline.
    assert min_required_for_category("B737", "cabin") == 3


# ── Counted-section per-role breakdown (CAPT/F/O · SCC/CC) ──────────────────
def test_flight_deck_per_role_b737_narrowbody():
    """Narrow-body: 1 CAPT + 1 F/O regardless of duration."""
    fd = flight_deck_expected_by_role("B737", 1.0)
    assert fd == {"pilot_captain": 1, "pilot_first_officer": 1}
    fd2 = flight_deck_expected_by_role("B737", 10.0)
    assert fd2 == {"pilot_captain": 1, "pilot_first_officer": 1}


def test_flight_deck_per_role_widebody_augments_on_longhaul():
    """Wide-body short-haul: 1 CAPT + 1 F/O. Long-haul (≥8h): 1 CAPT + 3 F/O."""
    short = flight_deck_expected_by_role("B787", 2.0)
    assert short == {"pilot_captain": 1, "pilot_first_officer": 1}
    long = flight_deck_expected_by_role("B787", 9.5)
    assert long == {"pilot_captain": 1, "pilot_first_officer": 3}


def test_cabin_per_role_b737_matches_gendec():
    """B737-800 GenDec: 1 SCC + 4 CC."""
    cc = cabin_crew_expected_by_role("B737")
    assert cc == {"senior_cabin_crew": 1, "cabin_crew": 4}


def test_cabin_per_role_cr9_has_no_scc_slot():
    """CR9 cabin is too small for a dedicated SCC seat — all are CC."""
    cc = cabin_crew_expected_by_role("CR9")
    assert cc["senior_cabin_crew"] == 0
    assert cc["cabin_crew"] == 2


def test_cabin_per_role_widebody():
    cc = cabin_crew_expected_by_role("B777")
    assert cc["senior_cabin_crew"] == 1
    assert cc["cabin_crew"] == 11                       # 12 ceiling − 1 SCC
