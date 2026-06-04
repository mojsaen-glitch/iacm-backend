"""Per-aircraft crew complement — backend mirror of lib/core/constants/fleet_spec.dart.

Authoritative source for "how many of each position a flight may carry". Used by
the assignment endpoint to REJECT over-staffing (a flight takes only its required
complement per category — pilots / cabin / engineer — never more), unless the
assigner explicitly overrides.

Rules (same as the Flutter FleetCatalog):
  • Wide-body (777/747/A330/B787/A380): 2 pilots short-haul, 4 pilots when the
    block time is ≥ 8h (augmented crew). Narrow-body: always 2 pilots.
  • Cabin ceiling = max attendants (full-service planning value).
  • Engineers: 0 for the modern fleet.
"""
from __future__ import annotations

# code → (min_pilots, max_pilots, min_attendants, max_attendants, engineers)
# min_attendants is the regulatory safety floor (by exit count); max is the
# full-service ceiling. Mirrors lib/core/constants/fleet_spec.dart.
#
# NOTE — B737-800 cabin max bumped 4→5 to fit the GenDec template (1 SCC + 4 CC).
_FLEET = {
    "B777": (2, 4, 8, 12, 0),
    "B747": (2, 4, 10, 14, 0),
    "A330": (2, 4, 7, 10, 0),
    "B787": (2, 4, 7, 10, 0),
    "A321": (2, 2, 4, 5, 0),
    "A320": (2, 2, 3, 4, 0),
    "B737": (2, 2, 3, 5, 0),
    "CR9":  (2, 2, 1, 2, 0),
    "A380": (2, 4, 14, 18, 0),
}
_GENERIC = (2, 2, 3, 4, 0)  # unknown type → narrow-body baseline

# Operational (NOT counted) complement per aircraft type.
# These roles are linked to the flight (light check only) and shown on the GenDec
# but do NOT gate publish — only flight_deck + cabin_crew gate. A missing
# operational position surfaces an advisory warning, never a block.
#
# Authoritative template — IAW 911/912 GenDec for B737-800:
#   1 CAPT + 1 F/O + 1 SCC + 4 CC + 1 AME + 1 L/SH + 3 IFSO.
# Other aircraft types use reasonable defaults (1 AME · 1 L/SH · IFSO 1–3 by size).
_OPERATIONAL: dict[str, dict[str, int]] = {
    # narrow-body short/medium-haul
    "B737": {"ame": 1, "lsh": 1, "ifso": 3, "obs": 0, "us": 0, "tech": 0},
    "A320": {"ame": 1, "lsh": 1, "ifso": 2, "obs": 0, "us": 0, "tech": 0},
    "A321": {"ame": 1, "lsh": 1, "ifso": 2, "obs": 0, "us": 0, "tech": 0},
    "CR9":  {"ame": 1, "lsh": 1, "ifso": 1, "obs": 0, "us": 0, "tech": 0},
    # wide-body long-haul (more IFSOs)
    "A330": {"ame": 1, "lsh": 1, "ifso": 3, "obs": 0, "us": 0, "tech": 0},
    "B787": {"ame": 1, "lsh": 1, "ifso": 3, "obs": 0, "us": 0, "tech": 0},
    "B777": {"ame": 1, "lsh": 1, "ifso": 3, "obs": 0, "us": 0, "tech": 0},
    "B747": {"ame": 1, "lsh": 1, "ifso": 3, "obs": 0, "us": 0, "tech": 0},
    "A380": {"ame": 1, "lsh": 1, "ifso": 3, "obs": 0, "us": 0, "tech": 0},
}
_OPERATIONAL_GENERIC = {"ame": 1, "lsh": 1, "ifso": 1, "obs": 0, "us": 0, "tech": 0}

# Map operational template keys → canonical role keys (crew_roles registry).
# Used to render "expected vs assigned" per role in the roster endpoint.
OPERATIONAL_KEY_TO_ROLE: dict[str, str] = {
    "ame":  "aircraft_maintenance_engineer",
    "lsh":  "load_sheet_officer",
    "ifso": "in_flight_security_officer",
    "obs":  "observer",
    "us":   "security_staff",
    "tech": "technical_staff",
}

# Rank → complement category. Mirrors _cat() in scheduling_timeline_screen.dart.
_PILOT_RANKS = {
    "CAPTAIN", "FIRST_OFFICER", "SECOND_OFFICER", "CO_PILOT",
    "PIC", "SIC", "SICN", "CPT", "FO", "SO",
}
_ENGINEER_RANKS = {"FLIGHT_ENGINEER", "F/E", "FE"}
_CABIN_RANKS = {
    "CHIEF", "PURSER", "SENIOR", "CABIN_CREW", "CABIN",
    "CC", "CCN", "SCC", "FCC", "FA",
}
# Pilot-in-command ranks — a flight must have at least one of these.
_CAPTAIN_RANKS = {"CAPTAIN", "PIC", "CPT"}


def category_for_rank(rank: str | None) -> str:
    """Complement-counting category: 'pilot' | 'cabin' | 'other'.

    Delegates to the role registry (single source of truth). ONLY pilots and
    cabin count toward the aircraft complement; every operational role
    (engineer/AME, ground, security, observer, …) is 'other' and never counted.
    """
    from app.core.crew_roles import complement_group
    g = complement_group(rank)
    return g if g in ("pilot", "cabin") else "other"


def is_captain_rank(rank: str | None) -> bool:
    """True for a pilot-in-command rank."""
    from app.core.crew_roles import is_captain
    return is_captain(rank)


def required_for_category(aircraft_type: str | None, category: str,
                          duration_hours: float | None) -> int | None:
    """Max crew this flight may carry in `category` (the over-staffing ceiling).

    Returns None for 'other' (no complement limit defined — never capped).
    """
    spec = _FLEET.get((aircraft_type or "").upper(), _GENERIC)
    min_p, max_p, min_a, max_a, eng = spec
    if category == "pilot":
        # Augmented crew only for wide-body (max>min) long-haul (≥8h).
        if max_p <= min_p:
            return min_p
        return max_p if (duration_hours or 0) >= 8 else min_p
    if category == "cabin":
        return max_a
    if category == "engineer":
        return eng
    return None


def min_required_for_category(aircraft_type: str | None, category: str) -> int:
    """Minimum crew a flight MUST carry in `category` (the safety floor) before
    it can be published. Pilots → minimum cockpit; cabin → exit-count floor;
    engineer → 0 on the modern fleet."""
    spec = _FLEET.get((aircraft_type or "").upper(), _GENERIC)
    min_p, _max_p, min_a, _max_a, eng = spec
    if category == "pilot":
        return min_p
    if category == "cabin":
        return min_a
    if category == "engineer":
        return eng
    return 0


def operational_complement_for(aircraft_type: str | None) -> dict[str, int]:
    """Per-aircraft operational (NOT counted) complement template.

    Returns a dict mapping short keys (ame/lsh/ifso/obs/us/tech) to the
    expected count for this aircraft type. These positions are SHOWN on the
    GenDec and surfaced as advisory expectations (warning if unfilled) but
    never block publish — only flight_deck + cabin_crew gate that.

    Use ``OPERATIONAL_KEY_TO_ROLE`` to translate keys to canonical
    ``crew.rank`` role values (registry-aligned).
    """
    spec = _OPERATIONAL.get((aircraft_type or "").upper())
    if spec is None:
        return dict(_OPERATIONAL_GENERIC)
    return dict(spec)


def operational_expected_by_role(aircraft_type: str | None) -> dict[str, int]:
    """Same template but keyed by canonical role (matches crew_roles registry).

    Example for B737:
        {
          "aircraft_maintenance_engineer": 1,
          "load_sheet_officer": 1,
          "in_flight_security_officer": 3,
          "observer": 0, "security_staff": 0, "technical_staff": 0,
        }
    """
    short = operational_complement_for(aircraft_type)
    return {OPERATIONAL_KEY_TO_ROLE[k]: v for k, v in short.items()}


# ── Counted sections (flight_deck / cabin_crew) — per-role breakdown ────────
def flight_deck_expected_by_role(aircraft_type: str | None,
                                 duration_hours: float | None = None) -> dict[str, int]:
    """Per-role expected complement for the flight_deck section.

    The cockpit always has exactly 1 captain; the rest of the min cockpit are
    first officers. On wide-body long-haul (≥ 8h) the cockpit augments and the
    extra crew are counted as additional F/Os.
    """
    spec = _FLEET.get((aircraft_type or "").upper(), _GENERIC)
    min_p, max_p, _min_a, _max_a, _eng = spec
    # Augmented crew on wide-body long-haul.
    pilots = max_p if (max_p > min_p and (duration_hours or 0) >= 8) else min_p
    fo = max(pilots - 1, 0)
    return {"pilot_captain": 1, "pilot_first_officer": fo}


def cabin_crew_expected_by_role(aircraft_type: str | None) -> dict[str, int]:
    """Per-role expected complement for the cabin_crew section.

    Always 1 Senior Cabin Crew (purser); the remaining seats are regular CC up
    to the cabin ceiling (e.g. B737-800 → 1 SCC + 4 CC = 5).
    CR9 has no SCC slot (small narrow-body) → all cabin seats are CC.
    """
    spec = _FLEET.get((aircraft_type or "").upper(), _GENERIC)
    _min_p, _max_p, _min_a, max_a, _eng = spec
    if max_a <= 2:                          # CR9 — no SCC slot
        return {"senior_cabin_crew": 0, "cabin_crew": max_a}
    return {"senior_cabin_crew": 1, "cabin_crew": max(max_a - 1, 0)}
