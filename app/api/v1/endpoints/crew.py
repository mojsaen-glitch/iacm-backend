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


@router.put("/{crew_id}")
async def update_crew_put(crew_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """PUT alias for PATCH — accepts full or partial update."""
    return await update_crew(crew_id, data, current_user, sb)


@router.delete("/{crew_id}", status_code=204)
async def delete_crew(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Delete a crew member. Admin only."""
    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")
    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)
    # Remove assignments first to avoid FK violations
    sb.table("assignments").delete().eq("crew_id", crew_id).execute()
    sb.table("crew").delete().eq("id", crew_id).execute()


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


@router.post("/{crew_id}/create-account")
async def create_crew_account(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Auto-create a system login account for a crew member. Admin only."""
    from app.core.security import get_password_hash
    from fastapi import HTTPException

    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")

    # Get crew member
    res = sb.table("crew").select("*").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("Crew member", crew_id)
    crew = res.data[0]

    employee_id = crew.get("employee_id", "").strip()
    if not employee_id:
        raise HTTPException(status_code=400, detail="الرقم الوظيفي مطلوب لإنشاء الحساب")

    # Generate credentials
    email = f"{employee_id.lower()}@iraqiairways.iq"
    password = f"IA@{employee_id}"

    # Check if account already exists
    existing = sb.table("users").select("id,email").eq("email", email).execute()
    if existing.data:
        # Return existing account info
        return {"email": email, "password": None, "already_exists": True, "user_id": existing.data[0]["id"]}

    # Create the user account
    new_user = {
        "email": email,
        "hashed_password": get_password_hash(password),
        "name_ar": crew.get("full_name_ar", ""),
        "name_en": crew.get("full_name_en", ""),
        "role": "crew",
        "company_id": current_user["company_id"],
        "crew_id": crew_id,
        "is_active": True,
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    result = sb.table("users").insert(new_user).execute()
    user = result.data[0] if result.data else {}

    return {
        "email": email,
        "password": password,
        "already_exists": False,
        "user_id": user.get("id"),
    }
