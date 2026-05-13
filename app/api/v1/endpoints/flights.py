import uuid, math
from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError

router = APIRouter(prefix="/flights", tags=["Flights"])


@router.get("")
async def list_flights(
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
):
    query = sb.table("flights").select("*", count="exact").eq("company_id", current_user["company_id"])
    if status:
        query = query.eq("status", status)

    skip = (page - 1) * page_size
    result = query.order("departure_time", desc=False).range(skip, skip + page_size - 1).execute()
    total = result.count or 0

    return {
        "items": result.data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
    }


@router.post("", status_code=201)
async def create_flight(data: dict, current_user: CurrentUser, sb: SbClient):
    dep = datetime.fromisoformat(data["departure_time"].replace("Z", "+00:00"))
    arr = datetime.fromisoformat(data["arrival_time"].replace("Z", "+00:00"))
    duration = round((arr - dep).total_seconds() / 3600, 2)

    data["id"] = str(uuid.uuid4())
    data["company_id"] = current_user["company_id"]
    data["duration_hours"] = duration
    data.setdefault("status", "scheduled")
    data.setdefault("publish_status", "draft")
    data.setdefault("crew_required", 4)
    data.setdefault("delay_minutes", 0)
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("flights").insert(data).execute()
    return result.data[0] if result.data else {}


@router.get("/{flight_id}")
async def get_flight(flight_id: str, current_user: CurrentUser, sb: SbClient):
    result = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not result.data:
        raise NotFoundError("Flight", flight_id)
    return result.data[0]


@router.patch("/{flight_id}")
async def update_flight(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Flight", flight_id)

    if "departure_time" in data or "arrival_time" in data:
        flight = sb.table("flights").select("departure_time,arrival_time").eq("id", flight_id).execute().data[0]
        dep_str = data.get("departure_time", flight["departure_time"])
        arr_str = data.get("arrival_time", flight["arrival_time"])
        dep = datetime.fromisoformat(str(dep_str).replace("Z", "+00:00"))
        arr = datetime.fromisoformat(str(arr_str).replace("Z", "+00:00"))
        data["duration_hours"] = round((arr - dep).total_seconds() / 3600, 2)

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("flights").update(data).eq("id", flight_id).execute()
    return result.data[0] if result.data else {}


@router.post("/{flight_id}/publish")
async def publish_flight(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """
    Publish a flight for crew assignment.
    - Changes publish_status to 'published'
    - Sends in-app notification to all allocators in the same company
    """
    flight_res = sb.table("flights").select("*") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    if flight.get("publish_status") == "published":
        return flight  # already published

    # Update flight
    updated = sb.table("flights").update({
        "publish_status": "published",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", flight_id).execute()

    # Notify all allocators (cabin + cockpit + ground + crew_allocator)
    allocator_roles = [
        "crew_allocator", "cabin_allocator",
        "cockpit_allocator", "ground_allocator",
        "ops_manager", "admin",
    ]
    users_res = sb.table("users").select("id,role") \
        .eq("company_id", current_user["company_id"]) \
        .eq("is_active", True).execute()

    flight_num = flight.get("flight_number", "")
    dep        = flight.get("departure_time", "")[:16].replace("T", " ")
    origin     = flight.get("origin_code", "")
    dest       = flight.get("destination_code", "")
    msg_ar     = f"رحلة جديدة تحتاج تكليف طاقم: {flight_num} ({origin}→{dest}) في {dep}"
    msg_en     = f"New flight needs crew assignment: {flight_num} ({origin}→{dest}) at {dep}"

    notifs = []
    for u in (users_res.data or []):
        if u["role"] in allocator_roles:
            notifs.append({
                "id":          str(uuid.uuid4()),
                "user_id":     u["id"],
                "type":        "flight_published",
                "title_ar":    "رحلة جديدة للتكليف",
                "title_en":    "New Flight for Assignment",
                "message_ar":  msg_ar,
                "message_en":  msg_en,
                "reference_id": flight_id,
                "reference_type": "flight",
                "is_read":     False,
                "created_at":  datetime.now(timezone.utc).isoformat(),
            })
    if notifs:
        sb.table("notifications").insert(notifs).execute()

    return updated.data[0] if updated.data else flight


@router.get("/pending-assignment")
async def get_flights_pending_assignment(current_user: CurrentUser, sb: SbClient):
    """
    Returns published flights that still need crew assigned.
    Department allocators see this filtered to their relevance.
    """
    result = sb.table("flights").select("*") \
        .eq("company_id", current_user["company_id"]) \
        .eq("publish_status", "published") \
        .neq("status", "cancelled") \
        .order("departure_time", desc=False).execute()
    return result.data or []
