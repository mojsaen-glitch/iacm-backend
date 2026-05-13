import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ConflictError, FTLViolationError, CrewBlockedError, ForbiddenError
from app.core.config import settings
from app.core.compliance_engine import ComplianceEngine, IRAQI_AIRPORTS

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
    # Step 1: get assignments
    q = sb.table("assignments").select("*")
    if flight_id:
        q = q.eq("flight_id", flight_id)
    if crew_id:
        q = q.eq("crew_id", crew_id)
    result = q.limit(page_size).execute()
    rows = result.data or []

    if not rows:
        return []

    # Step 2: fetch flight details for each unique flight_id
    flight_ids = list({r["flight_id"] for r in rows if r.get("flight_id")})
    flights_map = {}
    if flight_ids:
        fres = sb.table("flights").select("*").in_("id", flight_ids).execute()
        for f in (fres.data or []):
            flights_map[f["id"]] = f

    # Step 3: merge and apply date filter
    output = []
    for row in rows:
        flight = flights_map.get(row.get("flight_id"), {})
        row["flights"] = flight

        # Date filter
        dep = flight.get("departure_time", "")
        if dep and (from_date or to_date):
            dep_date = dep[:10]
            if from_date and dep_date < from_date:
                continue
            if to_date and dep_date > to_date:
                continue

        output.append(row)

    return output


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

    # ── Department restriction check ─────────────────────────
    # cabin_allocator can only assign cabin crew, cockpit_allocator only pilots, etc.
    user_role = current_user.get("role", "")
    crew_dept  = current_user.get("crew_department", "")
    crew_rank  = crew.get("rank", "")

    COCKPIT_RANKS = {"captain", "first_officer", "second_officer", "flight_engineer"}
    CABIN_RANKS   = {"chief", "purser", "senior", "cabin_crew"}
    GROUND_RANKS  = {"dispatcher", "ground_staff"}

    if user_role == "cabin_allocator" and crew_rank not in CABIN_RANKS and not is_override:
        raise ForbiddenError("مخصص الضيافة يمكنه فقط تكليف طاقم المقصورة")
    if user_role == "cockpit_allocator" and crew_rank not in COCKPIT_RANKS and not is_override:
        raise ForbiddenError("مخصص القيادة يمكنه فقط تكليف الطيارين")
    if user_role == "ground_allocator" and crew_rank not in GROUND_RANKS and not is_override:
        raise ForbiddenError("مخصص الأرضي يمكنه فقط تكليف الطاقم الأرضي")

    # Check duplicate
    dup = sb.table("assignments").select("id").eq("flight_id", flight_id).eq("crew_id", crew_id).execute()
    if dup.data:
        raise ConflictError(f"Crew member already assigned to flight {flight['flight_number']}")

    # ── Full Compliance Check ─────────────────────────────────
    if not is_override:
        dep_str = flight.get("departure_time", "")
        arr_str = flight.get("arrival_time", "")
        dep_dt = datetime.fromisoformat(dep_str.replace("Z", "+00:00")) if dep_str else None
        arr_dt = datetime.fromisoformat(arr_str.replace("Z", "+00:00")) if arr_str else None
        is_intl = (
            flight.get("origin_code", "").upper()      not in IRAQI_AIRPORTS or
            flight.get("destination_code", "").upper() not in IRAQI_AIRPORTS
        )

        engine = ComplianceEngine(sb)
        compliance = engine.check_crew(
            crew_id=crew_id,
            flight_id=flight_id,
            flight_departure=dep_dt,
            flight_arrival=arr_dt,
            is_international=is_intl,
        )

        if compliance.get("status") == "BLOCKED":
            reasons = "; ".join(compliance.get("blocking_reasons", ["Compliance violation"]))
            raise CrewBlockedError(crew.get("full_name_en", crew_id), reasons)

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
    saved  = result.data[0] if result.data else {}

    # ── Notify the crew member ───────────────────────────────
    try:
        # Find the user account linked to this crew member
        crew_user = sb.table("users").select("id").eq("crew_id", crew_id).execute()
        if crew_user.data:
            flight_num = flight.get("flight_number", "")
            dep_str    = flight.get("departure_time", "")[:16].replace("T", " ")
            origin     = flight.get("origin_code", "")
            dest       = flight.get("destination_code", "")
            sb.table("notifications").insert({
                "id":           str(uuid.uuid4()),
                "user_id":      crew_user.data[0]["id"],
                "type":         "crew_assigned",
                "title_ar":     "تم تكليفك برحلة",
                "title_en":     "You have been assigned to a flight",
                "message_ar":   f"تم تكليفك برحلة {flight_num} ({origin}→{dest}) في {dep_str}",
                "message_en":   f"You are assigned to flight {flight_num} ({origin}→{dest}) at {dep_str}",
                "reference_id": flight_id,
                "reference_type": "flight",
                "is_read":      False,
                "created_at":   datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception:
        pass  # notification failure should not block assignment

    return saved


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
