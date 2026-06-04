"""Crew role registry — GenDec-aligned 6-section model.

Proves the single source of truth (app.core.crew_roles) that the migration,
assign_crew, roster endpoint and UI all rely on:
  • each role → its correct section (flight_deck / cabin_crew /
    technical_operations / ground_operations / flight_security / observer)
  • ONLY flight_deck + cabin_crew count toward the aircraft complement
  • technical / ground / security / observer are operational-only (light check)
  • legacy ranks (captain, chief, ground_staff, flight_engineer…) still resolve

Run:  venv/Scripts/python -m pytest tests/test_assignment_category.py -q
"""
from app.core.crew_roles import (
    role_category, complement_group, counts_in_complement, is_operational_only,
    is_captain, assignment_bucket, normalize_role,
    CAT_FLIGHT_DECK, CAT_CABIN, CAT_TECHNICAL, CAT_GROUND, CAT_SECURITY, CAT_OBSERVER,
)
from app.core.fleet_complement import (
    category_for_rank, min_required_for_category, is_captain_rank,
)


# ── Section mapping for every role ─────────────────────────────────────────
def test_role_sections():
    cases = {
        "pilot_captain": CAT_FLIGHT_DECK,
        "pilot_first_officer": CAT_FLIGHT_DECK,
        "senior_cabin_crew": CAT_CABIN,
        "cabin_crew": CAT_CABIN,
        "aircraft_maintenance_engineer": CAT_TECHNICAL,
        "technical_staff": CAT_TECHNICAL,
        "load_sheet_officer": CAT_GROUND,
        "in_flight_security_officer": CAT_SECURITY,
        "security_staff": CAT_SECURITY,
        "observer": CAT_OBSERVER,
    }
    for role, cat in cases.items():
        assert role_category(role) == cat, role
        assert assignment_bucket(role) == cat, role


# ── Only flight deck + cabin count toward the complement ───────────────────
def test_counted_roles():
    assert complement_group("pilot_captain") == "pilot"
    assert complement_group("pilot_first_officer") == "pilot"
    assert complement_group("senior_cabin_crew") == "cabin"
    assert complement_group("cabin_crew") == "cabin"
    for r in ("pilot_captain", "pilot_first_officer", "senior_cabin_crew", "cabin_crew"):
        assert counts_in_complement(r) is True, r
        assert is_operational_only(r) is False, r


def test_operational_roles_never_counted():
    for r in ("aircraft_maintenance_engineer", "technical_staff", "load_sheet_officer",
              "in_flight_security_officer", "security_staff", "observer"):
        assert complement_group(r) is None, r
        assert counts_in_complement(r) is False, r
        assert is_operational_only(r) is True, r
        # fleet complement sees them as 'other' → zero minimum on any aircraft
        assert category_for_rank(r) == "other", r
        assert min_required_for_category("B737", category_for_rank(r)) == 0, r


def test_captain_gate():
    assert is_captain("pilot_captain") is True
    assert is_captain_rank("pilot_captain") is True
    assert is_captain("pilot_first_officer") is False
    assert is_captain("cabin_crew") is False


# ── fleet complement counting categories ───────────────────────────────────
def test_complement_categories():
    assert category_for_rank("pilot_captain") == "pilot"
    assert category_for_rank("cabin_crew") == "cabin"
    assert min_required_for_category("B737", "pilot") == 2
    assert min_required_for_category("B737", "cabin") == 3


# ── Legacy rank values still resolve to the right new role ─────────────────
def test_legacy_aliases():
    assert normalize_role("captain") == "pilot_captain"
    assert normalize_role("first_officer") == "pilot_first_officer"
    assert normalize_role("second_officer") == "pilot_first_officer"
    # engineer is now maintenance/technical → operational, NOT flight deck
    assert normalize_role("flight_engineer") == "aircraft_maintenance_engineer"
    assert role_category("flight_engineer") == CAT_TECHNICAL
    assert is_operational_only("flight_engineer") is True
    assert normalize_role("chief") == "senior_cabin_crew"
    assert normalize_role("purser") == "senior_cabin_crew"
    assert normalize_role("senior") == "senior_cabin_crew"
    # legacy generic ground → operational ground_operations
    assert role_category("ground_staff") == CAT_GROUND
    assert role_category("dispatcher") == CAT_GROUND
    assert is_operational_only("ground_staff") is True


def test_unknown_role_is_operational_failsafe():
    # Unknown role must never be mistaken for counted aircraft crew.
    assert role_category("some_future_role") == CAT_GROUND
    assert is_operational_only("some_future_role") is True
    assert role_category("") == CAT_GROUND
    assert role_category(None) == CAT_GROUND
