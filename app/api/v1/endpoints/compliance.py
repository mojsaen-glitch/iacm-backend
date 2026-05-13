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

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser, OpsManager
from app.core.compliance_engine import ComplianceEngine, IRAQI_AIRPORTS

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
# POST /compliance/check-crew/{crew_id}
# ─────────────────────────────────────────────────────────────
@router.post("/check-crew/{crew_id}")
async def check_crew_compliance(
    crew_id: str,
    current_user: CurrentUser,
    sb: SbClient,
):
    """Trigger a full compliance check for a crew member."""
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
    crew_id   = data.get("crew_id")
    flight_id = data.get("flight_id")

    if not crew_id or not flight_id:
        return {"error": "crew_id and flight_id are required"}

    # Load flight details
    flight_res = sb.table("flights").select("*").eq("id", flight_id).execute()
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
    crew_res = sb.table("crew") \
        .select("id,full_name_ar,full_name_en,employee_id,rank,status") \
        .eq("company_id", current_user["company_id"]) \
        .execute()
    crew_list = crew_res.data or []

    engine  = ComplianceEngine(sb)
    results = []

    for crew in crew_list:
        result = engine.check_crew(crew["id"])
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
