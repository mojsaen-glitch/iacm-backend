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
    existing = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Flight", flight_id)
    result = sb.table("flights").update({"publish_status": "published", "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", flight_id).execute()
    return result.data[0] if result.data else {}
