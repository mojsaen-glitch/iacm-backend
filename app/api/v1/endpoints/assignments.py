import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ConflictError, FTLViolationError, CrewBlockedError
from app.core.config import settings

router = APIRouter(prefix="/assignments", tags=["Crew Assignments"])


@router.get("")
async def get_assignments(
    current_user: CurrentUser,
    sb: SbClient,
    flight_id: Optional[str] = Query(None),
    crew_id: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    page_size: int = Query(100),
):
    """Get assignments filtered by flight_id or crew_id with optional date range."""
    query = sb.table("assignments").select(
        "*, flights(flight_number, origin, destination, departure_time, arrival_time, duration_hours, aircraft_type, status)"
    )

    if flight_id:
        query = query.eq("flight_id", flight_id)
    if crew_id:
        query = query.eq("crew_id", crew_id)

    result = query.limit(page_size).execute()
    rows = result.data or []

    # Apply date filtering in Python (Supabase FK join doesn't support date filters easily)
    if from_date or to_date:
        filtered = []
        for row in rows:
            flight = row.get("flights") or {}
            dep = flight.get("departure_time", "")
            if dep:
                dep_date = dep[:10]  # YYYY-MM-DD
                if from_date and dep_date < from_date:
                    continue
                if to_date and dep_date > to_date:
                    continue
            filtered.append(row)
        rows = filtered

    return rows


@router.post("", status_code=201)
async def assign_crew(data: dict, current_user: CurrentUser, sb: SbClient):
    flight_id = data["flight_id"]
    crew_id = data["crew_id"]
    is_override = data.get("is_override", False)

    # Validate flight
    flight_res = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    # Validate crew
    crew_res = sb.table("crew").select("*").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not crew_res.data:
        raise NotFoundError("Crew member", crew_id)
    crew = crew_res.data[0]

    # Check duplicate
    dup = sb.table("assignments").select("id").eq("flight_id", flight_id).eq("crew_id", crew_id).execute()
    if dup.data:
        raise ConflictError(f"Crew member already assigned to flight {flight['flight_number']}")

    # Check blocked
    if crew["status"] == "blocked" and not is_override:
        raise CrewBlockedError(crew["full_name_en"], crew.get("block_reason"))

    # FTL Check
    if not is_override:
        monthly = crew.get("monthly_flight_hours", 0)
        rolling_28 = crew.get("last_28day_hours", 0)
        duration = flight.get("duration_hours", 0)
        max_monthly = crew.get("max_monthly_hours", settings.MAX_MONTHLY_HOURS)
        violations = []
        if monthly + duration > max_monthly:
            violations.append(f"Monthly hours limit: {monthly:.1f} + {duration:.1f} > {max_monthly}")
        if rolling_28 + duration > 100:
            violations.append(f"28-day limit: {rolling_28:.1f} + {duration:.1f} > 100h")
        if violations:
            raise FTLViolationError("; ".join(violations))

    assignment = {
        "id": str(uuid.uuid4()),
        "flight_id": flight_id,
        "crew_id": crew_id,
        "assigned_by": current_user["id"],
        "assignment_type": data.get("assignment_type", "regular"),
        "is_override": is_override,
        "override_reason": data.get("override_reason") if is_override else None,
        "acknowledged": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    result = sb.table("assignments").insert(assignment).execute()
    return result.data[0] if result.data else {}


@router.delete("/{assignment_id}")
async def remove_assignment(assignment_id: str, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("assignments").select("id").eq("id", assignment_id).execute()
    if not existing.data:
        raise NotFoundError("Assignment", assignment_id)
    sb.table("assignments").delete().eq("id", assignment_id).execute()
    return {"message": "Assignment removed successfully", "success": True}


@router.get("/flight/{flight_id}")
async def get_flight_assignments(flight_id: str, current_user: CurrentUser, sb: SbClient):
    result = sb.table("assignments").select("*, crew(full_name_ar, full_name_en, rank, employee_id)").eq("flight_id", flight_id).execute()
    return result.data


@router.post("/{assignment_id}/acknowledge")
async def acknowledge_assignment(assignment_id: str, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("assignments").select("id").eq("id", assignment_id).execute()
    if not existing.data:
        raise NotFoundError("Assignment", assignment_id)
    result = sb.table("assignments").update({
        "acknowledged": True,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", assignment_id).execute()
    return result.data[0] if result.data else {}
