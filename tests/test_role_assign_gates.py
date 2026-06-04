"""Specialty scheduler / allocator RBAC gates — aligned to the GenDec role
registry. Each specialty scheduler may assign ONLY its own crew roles; broad
allocators cover their categories; general scheduler/admins are unrestricted.
Legacy crew.rank values resolve too (so existing data keeps working).

Run:  venv/Scripts/python -m pytest tests/test_role_assign_gates.py -q
"""
from app.api.v1.endpoints.assignments import (
    _role_may_assign_rank, _restricted_ranks,
    SCHED_ALLOWED_ROLES, ALLOC_ALLOWED_CATEGORIES,
)


# ── Specialty schedulers assign only their own role ────────────────────────
def test_sched_captain_only_captains():
    assert _role_may_assign_rank("sched_captain", "pilot_captain") is True
    assert _role_may_assign_rank("sched_captain", "captain") is True        # legacy
    assert _role_may_assign_rank("sched_captain", "pilot_first_officer") is False
    assert _role_may_assign_rank("sched_captain", "cabin_crew") is False


def test_sched_copilot_only_first_officers():
    assert _role_may_assign_rank("sched_copilot", "pilot_first_officer") is True
    assert _role_may_assign_rank("sched_copilot", "first_officer") is True   # legacy
    assert _role_may_assign_rank("sched_copilot", "pilot_captain") is False


def test_sched_engineer_covers_technical():
    assert _role_may_assign_rank("sched_engineer", "aircraft_maintenance_engineer") is True
    assert _role_may_assign_rank("sched_engineer", "technical_staff") is True
    assert _role_may_assign_rank("sched_engineer", "flight_engineer") is True  # legacy → AME
    assert _role_may_assign_rank("sched_engineer", "cabin_crew") is False


def test_sched_purser_and_cabin():
    assert _role_may_assign_rank("sched_purser", "senior_cabin_crew") is True
    assert _role_may_assign_rank("sched_purser", "chief") is True            # legacy
    assert _role_may_assign_rank("sched_purser", "cabin_crew") is False
    assert _role_may_assign_rank("sched_cabin", "cabin_crew") is True
    assert _role_may_assign_rank("sched_cabin", "senior_cabin_crew") is False


def test_sched_balance_security_extra():
    assert _role_may_assign_rank("sched_balance", "load_sheet_officer") is True
    assert _role_may_assign_rank("sched_balance", "dispatcher") is True      # legacy → load sheet
    assert _role_may_assign_rank("sched_security", "in_flight_security_officer") is True
    assert _role_may_assign_rank("sched_security", "security_staff") is True
    assert _role_may_assign_rank("sched_security", "load_sheet_officer") is False
    assert _role_may_assign_rank("sched_extra", "observer") is True
    assert _role_may_assign_rank("sched_extra", "pilot_captain") is False


# ── Broad allocators cover whole categories ────────────────────────────────
def test_cockpit_allocator_flight_deck():
    assert _role_may_assign_rank("cockpit_allocator", "pilot_captain") is True
    assert _role_may_assign_rank("cockpit_allocator", "pilot_first_officer") is True
    assert _role_may_assign_rank("cockpit_allocator", "cabin_crew") is False


def test_cabin_allocator_cabin():
    assert _role_may_assign_rank("cabin_allocator", "cabin_crew") is True
    assert _role_may_assign_rank("cabin_allocator", "senior_cabin_crew") is True
    assert _role_may_assign_rank("cabin_allocator", "pilot_captain") is False


def test_ground_allocator_all_operational():
    for r in ("aircraft_maintenance_engineer", "technical_staff", "load_sheet_officer",
              "in_flight_security_officer", "security_staff", "observer"):
        assert _role_may_assign_rank("ground_allocator", r) is True, r
    assert _role_may_assign_rank("ground_allocator", "pilot_captain") is False
    assert _role_may_assign_rank("ground_allocator", "cabin_crew") is False


# ── General scheduler / admins are unrestricted ────────────────────────────
def test_general_roles_unrestricted():
    for role in ("scheduler", "super_admin", "admin", "ops_manager", "scheduler_admin"):
        assert _role_may_assign_rank(role, "pilot_captain") is True
        assert _role_may_assign_rank(role, "observer") is True
        assert _restricted_ranks({"role": role}) is None


# ── _restricted_ranks includes BOTH new + legacy values for DB filtering ───
def test_restricted_ranks_includes_legacy():
    r = _restricted_ranks({"role": "sched_captain"})
    assert "pilot_captain" in r and "captain" in r
    r2 = _restricted_ranks({"role": "sched_security"})
    assert {"in_flight_security_officer", "security_staff"} <= r2
