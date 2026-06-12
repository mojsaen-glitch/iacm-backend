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


# ── Per-company template resolution (batch 2 of the company-settings plan) ──
# The three ops.fleet.* keys may override these templates PER COMPANY. With no
# sb/company context, no stored row, or ANY malformed value, every function
# below behaves EXACTLY as the constants above — fail-open by design, so a
# broken settings row can never weaken or break a safety gate.
_AUGMENT_THRESHOLD_HOURS_DEFAULT = 8


def _tupleize_fleet(raw: dict) -> tuple[dict, tuple]:
    """settings dict-form -> internal tuple form. Raises on malformation."""
    fleet: dict = {}
    for t, spec in raw.items():
        if t == "_generic":
            continue
        fleet[str(t).upper()] = (
            int(spec["min_pilots"]), int(spec["max_pilots"]),
            int(spec["min_cabin"]), int(spec["max_cabin"]),
            int(spec["engineers"]),
        )
    g = raw.get("_generic")
    generic = (int(g["min_pilots"]), int(g["max_pilots"]), int(g["min_cabin"]),
               int(g["max_cabin"]), int(g["engineers"])) if g else _GENERIC
    if not fleet:
        raise ValueError("empty fleet template")
    return fleet, generic


def _resolved_templates(sb=None, company_id=None):
    """(fleet, generic, augment_h, operational, operational_generic) for this
    company — today's constants when unset/unreadable (never raises)."""
    if sb is None or company_id is None:
        return (_FLEET, _GENERIC, _AUGMENT_THRESHOLD_HOURS_DEFAULT,
                _OPERATIONAL, _OPERATIONAL_GENERIC)
    from app.core.company_settings import get_company_setting
    try:
        fleet, generic = _tupleize_fleet(
            get_company_setting(sb, company_id, "ops.fleet.complement"))
    except Exception:
        fleet, generic = _FLEET, _GENERIC
    try:
        thr = float(get_company_setting(
            sb, company_id, "ops.fleet.augment_threshold_hours"))
        if thr <= 0:
            raise ValueError
    except Exception:
        thr = _AUGMENT_THRESHOLD_HOURS_DEFAULT
    try:
        raw = get_company_setting(
            sb, company_id, "ops.fleet.operational_complement")
        blank = {k: 0 for k in _OPERATIONAL_GENERIC}
        op = {str(t).upper(): {**blank,
                               **{k: int(v) for k, v in (spec or {}).items()}}
              for t, spec in raw.items() if t != "_generic"}
        opg = {**blank,
               **{k: int(v) for k, v in (raw.get("_generic")
                                         or _OPERATIONAL_GENERIC).items()}}
        if not op:
            raise ValueError("empty operational template")
    except Exception:
        op, opg = _OPERATIONAL, _OPERATIONAL_GENERIC
    return fleet, generic, thr, op, opg


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
                          duration_hours: float | None, *,
                          sb=None, company_id=None) -> int | None:
    """Max crew this flight may carry in `category` (the over-staffing ceiling).

    Returns None for 'other' (no complement limit defined — never capped).
    """
    fleet, generic, augment_h, _op, _opg = _resolved_templates(sb, company_id)
    spec = fleet.get((aircraft_type or "").upper(), generic)
    min_p, max_p, min_a, max_a, eng = spec
    if category == "pilot":
        # Augmented crew only for wide-body (max>min) long-haul (≥ threshold).
        if max_p <= min_p:
            return min_p
        return max_p if (duration_hours or 0) >= augment_h else min_p
    if category == "cabin":
        return max_a
    if category == "engineer":
        return eng
    return None


def min_required_for_category(aircraft_type: str | None, category: str, *,
                              sb=None, company_id=None) -> int:
    """Minimum crew a flight MUST carry in `category` (the safety floor) before
    it can be published. Pilots → minimum cockpit; cabin → exit-count floor;
    engineer → 0 on the modern fleet."""
    fleet, generic, _h, _op, _opg = _resolved_templates(sb, company_id)
    spec = fleet.get((aircraft_type or "").upper(), generic)
    min_p, _max_p, min_a, _max_a, eng = spec
    if category == "pilot":
        return min_p
    if category == "cabin":
        return min_a
    if category == "engineer":
        return eng
    return 0


def operational_complement_for(aircraft_type: str | None, *,
                               sb=None, company_id=None) -> dict[str, int]:
    """Per-aircraft operational (NOT counted) complement template.

    Returns a dict mapping short keys (ame/lsh/ifso/obs/us/tech) to the
    expected count for this aircraft type. These positions are SHOWN on the
    GenDec and surfaced as advisory expectations (warning if unfilled) but
    never block publish — only flight_deck + cabin_crew gate that.

    Use ``OPERATIONAL_KEY_TO_ROLE`` to translate keys to canonical
    ``crew.rank`` role values (registry-aligned).
    """
    _f, _g, _h, op, opg = _resolved_templates(sb, company_id)
    spec = op.get((aircraft_type or "").upper())
    if spec is None:
        return dict(opg)
    return dict(spec)


def operational_expected_by_role(aircraft_type: str | None, *,
                                 sb=None, company_id=None) -> dict[str, int]:
    """Same template but keyed by canonical role (matches crew_roles registry).

    Example for B737:
        {
          "aircraft_maintenance_engineer": 1,
          "load_sheet_officer": 1,
          "in_flight_security_officer": 3,
          "observer": 0, "security_staff": 0, "technical_staff": 0,
        }
    """
    short = operational_complement_for(aircraft_type, sb=sb,
                                       company_id=company_id)
    return {OPERATIONAL_KEY_TO_ROLE[k]: v for k, v in short.items()}


# ── Counted sections (flight_deck / cabin_crew) — per-role breakdown ────────
def flight_deck_expected_by_role(aircraft_type: str | None,
                                 duration_hours: float | None = None, *,
                                 sb=None, company_id=None) -> dict[str, int]:
    """Per-role expected complement for the flight_deck section.

    The cockpit always has exactly 1 captain; the rest of the min cockpit are
    first officers. On wide-body long-haul (≥ 8h) the cockpit augments and the
    extra crew are counted as additional F/Os.
    """
    fleet, generic, augment_h, _op, _opg = _resolved_templates(sb, company_id)
    spec = fleet.get((aircraft_type or "").upper(), generic)
    min_p, max_p, _min_a, _max_a, _eng = spec
    # Augmented crew on wide-body long-haul.
    pilots = max_p if (max_p > min_p and (duration_hours or 0) >= augment_h) else min_p
    fo = max(pilots - 1, 0)
    return {"pilot_captain": 1, "pilot_first_officer": fo}


def cabin_crew_expected_by_role(aircraft_type: str | None, *,
                                sb=None, company_id=None) -> dict[str, int]:
    """Per-role expected complement for the cabin_crew section.

    Always 1 Senior Cabin Crew (purser); the remaining seats are regular CC up
    to the cabin ceiling (e.g. B737-800 → 1 SCC + 4 CC = 5).
    CR9 has no SCC slot (small narrow-body) → all cabin seats are CC.
    """
    fleet, generic, _h, _op, _opg = _resolved_templates(sb, company_id)
    spec = fleet.get((aircraft_type or "").upper(), generic)
    _min_p, _max_p, _min_a, max_a, _eng = spec
    if max_a <= 2:                          # CR9 — no SCC slot
        return {"senior_cabin_crew": 0, "cabin_crew": max_a}
    return {"senior_cabin_crew": 1, "cabin_crew": max(max_a - 1, 0)}
