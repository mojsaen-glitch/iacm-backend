"""Crew role registry — single source of truth for GenDec-aligned crew roles.

Each role (stored in ``crew.rank``) maps to:
  • category            — one of 6 flight-roster sections
  • complement_group    — 'pilot' | 'cabin' | None.  ONLY pilot/cabin count
                          toward the aircraft complement / minimum crew.
  • code / label_ar / label_en — display.

Roles whose complement_group is None (technical / ground / security / observer)
are linked to a flight OPERATIONALLY — they are NOT aircraft crew: they never
count, never gate min-crew, are exempt from FTL/FDP/rest/qualification, and get
only a LIGHT check on assignment (active record + active account + not blocked).

Legacy ``crew.rank`` values (captain, chief, ground_staff, …) are normalised to
the new roles so existing data keeps working.
"""
from __future__ import annotations

# ── The 6 flight-roster sections (display order) ──────────────────────────
CAT_FLIGHT_DECK = "flight_deck"
CAT_CABIN       = "cabin_crew"
CAT_TECHNICAL   = "technical_operations"
CAT_GROUND      = "ground_operations"
CAT_SECURITY    = "flight_security"
CAT_OBSERVER    = "observer"

CATEGORY_ORDER = [
    CAT_FLIGHT_DECK, CAT_CABIN, CAT_TECHNICAL,
    CAT_GROUND, CAT_SECURITY, CAT_OBSERVER,
]

# ── Role spec. complement_group: 'pilot'|'cabin' are counted; None = operational
_ROLES: dict[str, dict] = {
    "pilot_captain":                 {"code": "CAPT", "category": CAT_FLIGHT_DECK, "complement_group": "pilot", "label_ar": "قائد الطائرة",            "label_en": "Captain"},
    "pilot_first_officer":           {"code": "F/O",  "category": CAT_FLIGHT_DECK, "complement_group": "pilot", "label_ar": "مساعد طيار",              "label_en": "First Officer"},
    "senior_cabin_crew":             {"code": "SCC",  "category": CAT_CABIN,       "complement_group": "cabin", "label_ar": "كبير طاقم المقصورة",       "label_en": "Senior Cabin Crew"},
    "cabin_crew":                    {"code": "CC",   "category": CAT_CABIN,       "complement_group": "cabin", "label_ar": "مضيف جوي",                "label_en": "Cabin Crew"},
    "aircraft_maintenance_engineer": {"code": "AME",  "category": CAT_TECHNICAL,   "complement_group": None,    "label_ar": "مهندس صيانة الطائرات",    "label_en": "Aircraft Maintenance Engineer"},
    "technical_staff":               {"code": "Tech", "category": CAT_TECHNICAL,   "complement_group": None,    "label_ar": "فني مرافق",               "label_en": "Technical Staff"},
    "load_sheet_officer":            {"code": "L/SH", "category": CAT_GROUND,      "complement_group": None,    "label_ar": "مسؤول التحميل/اللودشيت",  "label_en": "Load Sheet Officer"},
    "in_flight_security_officer":    {"code": "IFSO", "category": CAT_SECURITY,    "complement_group": None,    "label_ar": "ضابط أمن الرحلة",         "label_en": "In-Flight Security Officer"},
    "security_staff":                {"code": "US",   "category": CAT_SECURITY,    "complement_group": None,    "label_ar": "موظف أمني",               "label_en": "Security Staff"},
    "observer":                      {"code": "OBS",  "category": CAT_OBSERVER,    "complement_group": None,    "label_ar": "مراقب/ملاحظ",             "label_en": "Observer"},
}

# ── Legacy / short-code crew.rank → new role key ──────────────────────────
_LEGACY: dict[str, str] = {
    "captain": "pilot_captain", "pic": "pilot_captain", "cpt": "pilot_captain",
    "first_officer": "pilot_first_officer", "second_officer": "pilot_first_officer",
    "sic": "pilot_first_officer", "sicn": "pilot_first_officer", "co_pilot": "pilot_first_officer",
    "fo": "pilot_first_officer", "f/o": "pilot_first_officer", "so": "pilot_first_officer",
    # Engineer is maintenance (technical / operational) per ops decision.
    "flight_engineer": "aircraft_maintenance_engineer", "fe": "aircraft_maintenance_engineer",
    "f/e": "aircraft_maintenance_engineer", "ame": "aircraft_maintenance_engineer",
    "tech": "technical_staff",
    "chief": "senior_cabin_crew", "chf": "senior_cabin_crew",
    "purser": "senior_cabin_crew", "pur": "senior_cabin_crew",
    "senior": "senior_cabin_crew", "scc": "senior_cabin_crew",
    "cc": "cabin_crew", "cabin": "cabin_crew", "ccn": "cabin_crew", "fa": "cabin_crew", "fcc": "cabin_crew",
    "dispatcher": "load_sheet_officer", "dsp": "load_sheet_officer",
    "lsh": "load_sheet_officer", "l/sh": "load_sheet_officer",
    "ground_staff": "load_sheet_officer", "gnd": "load_sheet_officer",
    "balance": "load_sheet_officer",            # legacy sched_balance shorthand
    "ifso": "in_flight_security_officer",
    "security": "in_flight_security_officer",   # legacy sched_security shorthand
    "us": "security_staff",
    "obs": "observer",
    "extra": "observer",                        # legacy sched_extra shorthand
}


def normalize_role(rank: str | None) -> str:
    """Canonical role key for a stored crew.rank (handles new + legacy + codes)."""
    r = (rank or "").strip()
    if r in _ROLES:
        return r
    low = r.lower()
    if low in _ROLES:
        return low
    return _LEGACY.get(low, low)


def role_spec(rank: str | None) -> dict | None:
    return _ROLES.get(normalize_role(rank))


def role_category(rank: str | None) -> str:
    """One of the 6 sections. Unknown role → ground_operations (operational,
    never counted) — a safe fail-closed default for the complement."""
    spec = role_spec(rank)
    return spec["category"] if spec else CAT_GROUND


def assignment_bucket(rank: str | None) -> str:
    """Section stored in assignments.assignment_type — same as role_category."""
    return role_category(rank)


def complement_group(rank: str | None) -> str | None:
    """'pilot' | 'cabin' for counted aircraft crew; None for operational-only."""
    spec = role_spec(rank)
    return spec["complement_group"] if spec else None


def counts_in_complement(rank: str | None) -> bool:
    return complement_group(rank) is not None


def is_operational_only(rank: str | None) -> bool:
    """True for technical / ground / security / observer (and unknown) roles —
    linked to the flight but NOT aircraft crew (light check, no count)."""
    return complement_group(rank) is None


def is_captain(rank: str | None) -> bool:
    return normalize_role(rank) == "pilot_captain"


def roles_in_categories(categories) -> set[str]:
    """All role keys whose category is in `categories` (a set/list of the 6
    section constants). Used to narrow a specialty allocator's crew list."""
    cats = set(categories)
    return {r for r, spec in _ROLES.items() if spec["category"] in cats}


def expand_with_legacy(roles) -> set[str]:
    """Given canonical role keys, also include every legacy / short crew.rank
    value that normalizes to one of them. Use this when filtering a DB query on
    crew.rank (e.g. `.in_('rank', ...)`) so it matches BOTH the new GenDec roles
    and any old stored values (captain, chief, ground_staff, …)."""
    target = set(roles)
    out = set(target)
    for legacy, canon in _LEGACY.items():
        if canon in target:
            out.add(legacy)
    return out


def role_code(rank: str | None) -> str:
    spec = role_spec(rank)
    return spec["code"] if spec else (rank or "").upper()


def role_label(rank: str | None, arabic: bool = True) -> str:
    spec = role_spec(rank)
    if not spec:
        return rank or ""
    return spec["label_ar"] if arabic else spec["label_en"]
