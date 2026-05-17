import uuid, math
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ForbiddenError

router = APIRouter(prefix="/leave-requests", tags=["Leave Requests"])

MANAGER_ROLES = {"super_admin", "admin", "ops_manager"}


@router.get("")
async def list_leave_requests(
    current_user: CurrentUser,
    sb: SbClient,
    status: Optional[str] = Query(None),
    crew_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Managers see all company requests.
    Crew see only their own requests.
    """
    role = current_user.get("role", "")

    if role in MANAGER_ROLES:
        q = sb.table("leave_requests").select("*", count="exact")\
            .eq("company_id", current_user["company_id"])
        if status:
            q = q.eq("status", status)
        if crew_id:
            q = q.eq("crew_id", crew_id)
    else:
        # Crew can only see their own
        own_crew = current_user.get("crew_id")
        if not own_crew:
            return {"items": [], "total": 0, "page": 1, "total_pages": 1}
        q = sb.table("leave_requests").select("*", count="exact")\
            .eq("crew_id", own_crew)
        if status:
            q = q.eq("status", status)

    skip   = (page - 1) * page_size
    result = q.order("created_at", desc=True).range(skip, skip + page_size - 1).execute()
    total  = result.count or 0
    return {
        "items":       result.data,
        "total":       total,
        "page":        page,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
    }


@router.post("", status_code=201)
async def create_leave_request(data: dict, current_user: CurrentUser, sb: SbClient):
    """
    Crew submits a leave request.
    Managers can submit on behalf of crew by providing crew_id.
    """
    role = current_user.get("role", "")

    if role in MANAGER_ROLES:
        crew_id = data.get("crew_id", "").strip()
        if not crew_id:
            raise HTTPException(status_code=422, detail="crew_id مطلوب")
        crew_check = sb.table("crew").select("id").eq("id", crew_id)\
            .eq("company_id", current_user["company_id"]).execute()
        if not crew_check.data:
            raise NotFoundError("Crew member", crew_id)
    else:
        crew_id = current_user.get("crew_id", "")
        if not crew_id:
            raise ForbiddenError("حسابك غير مرتبط بسجل طاقم")

    if not data.get("leave_type"):
        raise HTTPException(status_code=422, detail="leave_type مطلوب")
    if not data.get("start_date") or not data.get("end_date"):
        raise HTTPException(status_code=422, detail="start_date و end_date مطلوبان")

    record = {
        "id":          str(uuid.uuid4()),
        "crew_id":     crew_id,
        "company_id":  current_user["company_id"],
        "leave_type":  data["leave_type"],
        "start_date":  data["start_date"],
        "end_date":    data["end_date"],
        "reason":      data.get("reason", ""),
        "status":      "pending",
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "updated_at":  datetime.now(timezone.utc).isoformat(),
    }

    result = sb.table("leave_requests").insert(record).execute()
    saved  = result.data[0] if result.data else record

    # Notify managers
    try:
        managers = sb.table("users").select("id,role")\
            .eq("company_id", current_user["company_id"])\
            .eq("is_active", True).execute()
        crew_info = sb.table("crew").select("full_name_ar")\
            .eq("id", crew_id).execute()
        name_ar = crew_info.data[0]["full_name_ar"] if crew_info.data else "عضو طاقم"

        notifs = []
        for u in (managers.data or []):
            if u["role"] in MANAGER_ROLES:
                notifs.append({
                    "id":             str(uuid.uuid4()),
                    "user_id":        u["id"],
                    "type":           "leave_request",
                    "title_ar":       "طلب إجازة جديد",
                    "title_en":       "New Leave Request",
                    "message_ar":     f"{name_ar} قدّم طلب إجازة ({data['leave_type']})",
                    "message_en":     f"{name_ar} submitted a leave request ({data['leave_type']})",
                    "reference_id":   saved["id"],
                    "reference_type": "leave",
                    "is_read":        False,
                    "created_at":     datetime.now(timezone.utc).isoformat(),
                })
        if notifs:
            sb.table("notifications").insert(notifs).execute()
    except Exception:
        pass

    return saved


@router.patch("/{request_id}/review")
async def review_leave_request(request_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Managers approve or reject a leave request."""
    if current_user["role"] not in MANAGER_ROLES:
        raise ForbiddenError("يتطلب صلاحية مدير")

    existing = sb.table("leave_requests").select("*")\
        .eq("id", request_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Leave request", request_id)

    action = data.get("action", "").lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action يجب أن يكون approve أو reject")

    new_status = "approved" if action == "approve" else "rejected"

    updated = sb.table("leave_requests").update({
        "status":       new_status,
        "reviewed_by":  current_user["id"],
        "reviewed_at":  datetime.now(timezone.utc).isoformat(),
        "review_notes": data.get("review_notes", ""),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }).eq("id", request_id).execute()

    # Notify crew member
    try:
        req = existing.data[0]
        crew_user = sb.table("users").select("id")\
            .eq("crew_id", req["crew_id"]).execute()
        if crew_user.data:
            result_ar = "تمت الموافقة على" if new_status == "approved" else "تم رفض"
            sb.table("notifications").insert({
                "id":             str(uuid.uuid4()),
                "user_id":        crew_user.data[0]["id"],
                "type":           "leave_reviewed",
                "title_ar":       f"{result_ar} طلب الإجازة",
                "title_en":       f"Leave Request {new_status.capitalize()}",
                "message_ar":     f"{result_ar} طلب إجازتك ({req.get('leave_type', '')})",
                "message_en":     f"Your leave request ({req.get('leave_type', '')}) has been {new_status}",
                "reference_id":   request_id,
                "reference_type": "leave",
                "is_read":        False,
                "created_at":     datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception:
        pass

    return updated.data[0] if updated.data else {}


@router.delete("/{request_id}", status_code=204)
async def cancel_leave_request(request_id: str, current_user: CurrentUser, sb: SbClient):
    """Crew can cancel their pending request. Managers can cancel any."""
    existing = sb.table("leave_requests").select("*").eq("id", request_id).execute()
    if not existing.data:
        raise NotFoundError("Leave request", request_id)

    req  = existing.data[0]
    role = current_user.get("role", "")

    if role not in MANAGER_ROLES:
        if req["crew_id"] != current_user.get("crew_id"):
            raise ForbiddenError("لا يمكنك إلغاء هذا الطلب")
        if req["status"] != "pending":
            raise HTTPException(status_code=400, detail="لا يمكن إلغاء طلب تمت مراجعته")

    sb.table("leave_requests").delete().eq("id", request_id).execute()
