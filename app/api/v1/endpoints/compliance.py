"""
Compliance API Endpoints
========================
Exposes the Compliance Engine over HTTP.

Routes:
  GET  /compliance/crew/{crew_id}/status    — full compliance check (no flight)
  POST /compliance/check-assignment         — check crew+flight combination
  GET  /compliance/blocked-crew             — all crew with status != GREEN
  POST /compliance/check-crew/{crew_id}     — same as GET /status (POST convenience)
"""

import logging
from datetime import datetime, date, timezone
from typing import Optional
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser, OpsManager
from app.core.compliance_engine import (
    ComplianceEngine, ComplianceStatus, Severity, IRAQI_AIRPORTS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/compliance", tags=["Compliance"])


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO datetime string, handling Z suffix."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _is_international(origin: str, destination: str) -> bool:
    return (
        origin.upper()      not in IRAQI_AIRPORTS or
        destination.upper() not in IRAQI_AIRPORTS
    )


# ─────────────────────────────────────────────────────────────
# GET /compliance/crew/{crew_id}/status
# ─────────────────────────────────────────────────────────────
@router.get("/crew/{crew_id}/status")
async def get_crew_compliance_status(
    crew_id: str,
    current_user: CurrentUser,
    sb: SbClient,
):
    """
    Full compliance check for a crew member — no specific flight.
    Returns GREEN / YELLOW / RED / BLOCKED with detailed issues.
    """
    engine = ComplianceEngine(sb)
    return engine.check_crew(crew_id)


# ─────────────────────────────────────────────────────────────
# Role gate — compliance checks expose docs / training expiry / FDP usage,
# which is sensitive PII. Restrict to operations + compliance staff.
# ─────────────────────────────────────────────────────────────
_COMPLIANCE_READERS = {
    "super_admin", "admin", "ops_manager", "scheduler",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "compliance_officer", "flight_movement", "flight_ops", "flight_operations",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}


def _ensure_compliance_reader(user: dict) -> None:
    if user.get("role") not in _COMPLIANCE_READERS:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("غير مصرح بإجراء فحص الامتثال")


# ─────────────────────────────────────────────────────────────
# GET /compliance/crew/{crew_id}/legality
# ─────────────────────────────────────────────────────────────
@router.get("/crew/{crew_id}/legality")
async def get_crew_legality(
    crew_id: str,
    current_user: CurrentUser,
    sb: SbClient,
    reference_time: Optional[str] = Query(None, description="ISO-8601 UTC; default = now"),
    window_start:   Optional[str] = Query(None, description="ISO-8601 UTC window start"),
    window_end:     Optional[str] = Query(None, description="ISO-8601 UTC window end"),
):
    """Live legality snapshot for the OCC countdown card — remaining FDP / duty /
    flight-time, minimum rest, next legal report time, and overall status.
    Read-only; computes against current assignments + FDP/rest/FTL rules."""
    _ensure_compliance_reader(current_user)
    from app.core.exceptions import NotFoundError
    crew_check = sb.table("crew").select("id").eq("id", crew_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)
    engine = ComplianceEngine(sb)
    return engine.crew_legality(
        crew_id,
        reference_time=_parse_dt(reference_time),
        window_start=_parse_dt(window_start),
        window_end=_parse_dt(window_end),
    )


# ─────────────────────────────────────────────────────────────
# GET /compliance/fdp-today  — roster-wide FDP board for the day
# ─────────────────────────────────────────────────────────────
@router.get("/fdp-today")
async def fdp_today(
    current_user: CurrentUser,
    sb: SbClient,
    date_str: Optional[str] = Query(None, alias="date"),
):
    """Every crew scheduled on the target Baghdad day with a compact FDP verdict
    (sectors, FDP used/remaining, previous rest, status). Worst status first."""
    _ensure_compliance_reader(current_user)
    on_date = None
    if date_str:
        try:
            on_date = date.fromisoformat(date_str)
        except (ValueError, TypeError):
            on_date = None
    rows = ComplianceEngine(sb).fdp_monitor_today(current_user["company_id"], on_date=on_date)
    return {"date": (on_date.isoformat() if on_date else None), "crew": rows, "count": len(rows)}


# ─────────────────────────────────────────────────────────────
# GET /compliance/fdp-monitor/{crew_id}
# ─────────────────────────────────────────────────────────────
@router.get("/fdp-monitor/{crew_id}")
async def fdp_monitor(
    crew_id: str,
    current_user: CurrentUser,
    sb: SbClient,
    date_str: Optional[str] = Query(None, alias="date",
                                    description="Baghdad-local day YYYY-MM-DD; default = today's/active duty"),
):
    """Schedule-linked FDP snapshot for one crew: their flights for the target
    day, report time, sectors, final arrival, FDP used/max/remaining, previous
    rest and the compliance verdict. All times UTC (UI renders Baghdad=UTC+3)."""
    _ensure_compliance_reader(current_user)
    from app.core.exceptions import NotFoundError
    crew_check = sb.table("crew").select("id").eq("id", crew_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)
    on_date = None
    if date_str:
        try:
            on_date = date.fromisoformat(date_str)
        except (ValueError, TypeError):
            on_date = None
    return ComplianceEngine(sb).fdp_monitor(crew_id, on_date=on_date)


# ─────────────────────────────────────────────────────────────
# POST /compliance/check-crew/{crew_id}
# ─────────────────────────────────────────────────────────────
@router.post("/check-crew/{crew_id}")
async def check_crew_compliance(
    crew_id: str,
    current_user: CurrentUser,
    sb: SbClient,
):
    """Trigger a full compliance check for a crew member."""
    _ensure_compliance_reader(current_user)
    # Verify the crew member belongs to the caller's company before invoking
    # the engine — the engine itself takes only crew_id, so without this guard
    # a cross-tenant lookup would succeed.
    from app.core.exceptions import NotFoundError
    crew_check = sb.table("crew").select("id").eq("id", crew_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)
    engine = ComplianceEngine(sb)
    return engine.check_crew(crew_id)


# ─────────────────────────────────────────────────────────────
# POST /compliance/check-assignment
# ─────────────────────────────────────────────────────────────
@router.post("/check-assignment")
async def check_assignment_compliance(
    data: dict,
    current_user: CurrentUser,
    sb: SbClient,
):
    """
    Check if assigning a crew member to a specific flight is compliant.

    Body:
      { "crew_id": "...", "flight_id": "..." }

    Returns full compliance result including:
      - All general checks (docs, training, hours, status)
      - Assignment conflict check
      - Rest period check
    """
    _ensure_compliance_reader(current_user)
    crew_id   = data.get("crew_id")
    flight_id = data.get("flight_id")

    if not crew_id or not flight_id:
        return {"error": "crew_id and flight_id are required"}

    # Load flight details — enforce company isolation
    flight_res = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        return {"error": f"Flight {flight_id} not found"}
    flight = flight_res.data[0]

    dep = _parse_dt(flight.get("departure_time"))
    arr = _parse_dt(flight.get("arrival_time"))
    intl = _is_international(
        flight.get("origin_code", ""),
        flight.get("destination_code", ""),
    )

    engine = ComplianceEngine(sb)
    result = engine.check_crew(
        crew_id=crew_id,
        flight_id=flight_id,
        flight_departure=dep,
        flight_arrival=arr,
        is_international=intl,
        flight_aircraft_type=flight.get("aircraft_type"),
    )

    # Attach flight info for convenience
    result["flight"] = {
        "id":             flight_id,
        "flight_number":  flight.get("flight_number"),
        "origin":         flight.get("origin_code"),
        "destination":    flight.get("destination_code"),
        "departure_time": flight.get("departure_time"),
        "arrival_time":   flight.get("arrival_time"),
        "is_international": intl,
    }
    return result


# ─────────────────────────────────────────────────────────────
# GET /compliance/blocked-crew
# ─────────────────────────────────────────────────────────────
@router.get("/blocked-crew")
async def get_blocked_crew(
    current_user: CurrentUser,
    sb: SbClient,
    status_filter: Optional[str] = Query(None, description="YELLOW|RED|BLOCKED"),
):
    """
    Returns all crew members whose compliance status is not GREEN.
    Useful for the compliance dashboard.

    Warning: runs a check per crew member — may be slow for large crews.
    Use for dashboard/reporting only, not per-request checks.
    """
    # A total DB failure here SHOULD surface as 500 — we can't reason about
    # compliance with no crew list. Per-crew failures below are isolated.
    crew_res = sb.table("crew") \
        .select("id,full_name_ar,full_name_en,employee_id,rank,status") \
        .eq("company_id", current_user["company_id"]) \
        .execute()
    crew_list = crew_res.data or []

    engine  = ComplianceEngine(sb)
    results = []

    for crew in crew_list:
        cid     = crew.get("id")
        name_ar = crew.get("full_name_ar", "")
        try:
            result = engine.check_crew(cid)
        except Exception as exc:
            # Fail-closed but ISOLATED: a bad record (missing data / invalid
            # time) must not 500 the whole board. Surface this crew member as
            # BLOCKED with a clear reason and keep checking the rest.
            logger.exception(
                "blocked-crew: compliance check failed for crew_id=%s name=%s",
                cid, name_ar,
            )
            _msg_ar = "تعذر فحص الامتثال لهذا الطاقم بسبب بيانات ناقصة أو وقت غير صالح"
            _msg_en = ("Could not run compliance check for this crew member "
                       "due to missing data or an invalid time")
            result = {
                "crew_id":          cid,
                "crew_name_ar":     name_ar,
                "crew_name_en":     crew.get("full_name_en", ""),
                "employee_id":      crew.get("employee_id", ""),
                "rank":             crew.get("rank", ""),
                "status":           ComplianceStatus.BLOCKED,
                "issues":           [{
                    "rule":        "compliance_check_error",
                    "severity":    Severity.BLOCKING,
                    "message_ar":  _msg_ar,
                    "message_en":  _msg_en,
                    "is_blocking": True,
                    "detail":      {"error": str(exc)},
                    "om_ref":      None,
                }],
                "blocking_count":   1,
                "critical_count":   0,
                "warning_count":    0,
                "info_count":       0,
                "blocking_reasons": [_msg_ar],
                "checked_at":       datetime.now(timezone.utc).isoformat(),
            }

        comp_status = result.get("status", "GREEN")

        # Apply optional filter
        if status_filter and comp_status != status_filter.upper():
            continue

        if comp_status != "GREEN":
            results.append(result)

    # Sort: BLOCKED first, then RED, then YELLOW
    order = {"BLOCKED": 0, "RED": 1, "YELLOW": 2}
    results.sort(key=lambda r: order.get(r.get("status", "YELLOW"), 99))
    return results


# ─────────────────────────────────────────────────────────────
# GET /compliance/summary
# ─────────────────────────────────────────────────────────────
@router.get("/summary")
async def get_compliance_summary(
    current_user: CurrentUser,
    sb: SbClient,
):
    """
    Quick summary count: how many crew are GREEN / YELLOW / RED / BLOCKED.
    Lighter than /blocked-crew because it doesn't return full details.
    """
    crew_res = sb.table("crew") \
        .select("id") \
        .eq("company_id", current_user["company_id"]) \
        .execute()
    crew_list = crew_res.data or []

    engine = ComplianceEngine(sb)
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0, "BLOCKED": 0}

    for crew in crew_list:
        result = engine.check_crew(crew["id"])
        s = result.get("status", "GREEN")
        counts[s] = counts.get(s, 0) + 1

    counts["total"] = len(crew_list)
    counts["non_green"] = counts["YELLOW"] + counts["RED"] + counts["BLOCKED"]
    return counts
