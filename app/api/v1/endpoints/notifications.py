import math
from datetime import datetime, timezone
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("")
async def list_notifications(
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    unread_only: bool = False,
):
    query = sb.table("notifications").select("*", count="exact").eq("target_user_id", current_user["id"])
    if unread_only:
        query = query.eq("is_read", False)
    skip = (page - 1) * page_size
    result = query.order("created_at", desc=True).range(skip, skip + page_size - 1).execute()
    total = result.count or 0
    return {
        "items": result.data,
        "total": total,
        "page": page,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
    }


@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, current_user: CurrentUser, sb: SbClient):
    sb.table("notifications").update({
        "is_read": True,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", notification_id).eq("target_user_id", current_user["id"]).execute()
    return {"message": "Marked as read"}


@router.post("/read-all")
async def mark_all_read(current_user: CurrentUser, sb: SbClient):
    sb.table("notifications").update({
        "is_read": True,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("target_user_id", current_user["id"]).eq("is_read", False).execute()
    return {"message": "All notifications marked as read"}
