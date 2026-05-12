import os, uuid
from typing import Optional
from fastapi import APIRouter, Query, UploadFile, File
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ConflictError, ForbiddenError
from app.core.config import settings
from datetime import datetime, timezone

router = APIRouter(prefix="/crew", tags=["Crew Management"])


@router.get("")
async def list_crew(
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    rank: Optional[str] = None,
    search: Optional[str] = None,
):
    query = sb.table("crew").select("*", count="exact").eq("company_id", current_user["company_id"])
    if status:
        query = query.eq("status", status)
    if rank:
        query = query.eq("rank", rank)
    if search:
        query = query.or_(f"full_name_ar.ilike.%{search}%,full_name_en.ilike.%{search}%,employee_id.ilike.%{search}%")

    skip = (page - 1) * page_size
    result = query.range(skip, skip + page_size - 1).execute()
    total = result.count or 0

    import math
    return {
        "items": result.data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
    }


@router.post("", status_code=201)
async def create_crew(data: dict, current_user: CurrentUser, sb: SbClient):
    if current_user["role"] not in ["super_admin", "admin", "ops_manager"] and not current_user.get("is_superuser"):
        raise ForbiddenError("Insufficient permissions")

    existing = sb.table("crew").select("id").eq("employee_id", data.get("employee_id", "")).execute()
    if existing.data:
        raise ConflictError(f"Employee ID '{data.get('employee_id')}' already exists")

    data["id"] = str(uuid.uuid4())
    data["company_id"] = current_user["company_id"]
    data.setdefault("status", "active")
    data.setdefault("monthly_flight_hours", 0)
    data.setdefault("total_flight_hours", 0)
    data.setdefault("max_monthly_hours", 100)
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("crew").insert(data).execute()
    return result.data[0] if result.data else {}


@router.get("/{crew_id}")
async def get_crew(crew_id: str, current_user: CurrentUser, sb: SbClient):
    result = sb.table("crew").select("*").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not result.data:
        raise NotFoundError("Crew member", crew_id)
    return result.data[0]


@router.patch("/{crew_id}")
async def update_crew(crew_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("crew").update(data).eq("id", crew_id).execute()
    return result.data[0] if result.data else {}


@router.post("/{crew_id}/block")
async def block_crew(crew_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)

    result = sb.table("crew").update({
        "status": "blocked",
        "block_reason": data.get("reason"),
        "blocked_by": current_user["id"],
        "blocked_on": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", crew_id).execute()
    return result.data[0] if result.data else {}


@router.post("/{crew_id}/unblock")
async def unblock_crew(crew_id: str, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)

    result = sb.table("crew").update({
        "status": "active",
        "block_reason": None,
        "blocked_by": None,
        "blocked_on": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", crew_id).execute()
    return result.data[0] if result.data else {}
