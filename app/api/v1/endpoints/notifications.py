import uuid, math
from datetime import datetime, timezone
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/notifications", tags=["Notifications"])

# Roles allowed to send notifications to others
SENDER_ROLES = {"super_admin", "admin", "ops_manager"}


# ── GET /notifications — يستلم المستخدم إشعاراته فقط ─────────────────────────
@router.get("")
async def list_notifications(
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    unread_only: bool = False,
):
    query = sb.table("notifications").select("*", count="exact").eq("user_id", current_user["id"])
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


# ── POST /notifications — إرسال إشعار (الأدمن والمدير فقط) ──────────────────
@router.post("", status_code=201)
async def send_notification(data: dict, current_user: CurrentUser, sb: SbClient):
    """إرسال إشعار لمستخدم أو لجميع مستخدمي الشركة. الأدمن والمدير فقط."""
    if current_user["role"] not in SENDER_ROLES:
        raise ForbiddenError("فقط الإدمن ومدير العمليات يمكنهم إرسال الإشعارات")

    company_id = current_user["company_id"]
    target_user_id = data.get("user_id")        # إشعار لشخص محدد
    send_to_all = data.get("send_to_all", False) # إشعار لجميع موظفي الشركة

    title_ar  = data.get("title_ar", "")
    title_en  = data.get("title_en", "")
    message_ar = data.get("message_ar", "")
    message_en = data.get("message_en", "")
    notif_type = data.get("type", "manual")
    reference_id   = data.get("reference_id")
    reference_type = data.get("reference_type")

    now = datetime.now(timezone.utc).isoformat()

    if send_to_all:
        # إرسال لجميع المستخدمين النشطين في الشركة
        users_res = sb.table("users").select("id").eq("company_id", company_id).eq("is_active", True).execute()
        target_ids = [u["id"] for u in (users_res.data or [])]
    elif target_user_id:
        # التحقق أن المستخدم المستهدف ينتمي لنفس الشركة
        user_check = sb.table("users").select("id").eq("id", target_user_id).eq("company_id", company_id).execute()
        if not user_check.data:
            raise NotFoundError("User", target_user_id)
        target_ids = [target_user_id]
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="يجب تحديد user_id أو تفعيل send_to_all")

    notifs = [
        {
            "id":             str(uuid.uuid4()),
            "user_id":        uid,
            "type":           notif_type,
            "title_ar":       title_ar,
            "title_en":       title_en,
            "message_ar":     message_ar,
            "message_en":     message_en,
            "reference_id":   reference_id,
            "reference_type": reference_type,
            "is_read":        False,
            "created_at":     now,
        }
        for uid in target_ids
    ]

    sb.table("notifications").insert(notifs).execute()
    return {"sent": len(notifs), "message": "تم إرسال الإشعارات بنجاح"}


# ── POST /notifications/{id}/read — قراءة إشعار ──────────────────────────────
@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, current_user: CurrentUser, sb: SbClient):
    """يسمح فقط لصاحب الإشعار بتحديده كمقروء."""
    result = sb.table("notifications").update({
        "is_read": True,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", notification_id).eq("user_id", current_user["id"]).execute()

    if not result.data:
        raise NotFoundError("Notification", notification_id)
    return {"message": "تم تحديد الإشعار كمقروء"}


# ── POST /notifications/read-all — قراءة جميع الإشعارات ──────────────────────
@router.post("/read-all")
async def mark_all_read(current_user: CurrentUser, sb: SbClient):
    """تحديد جميع إشعارات المستخدم الحالي كمقروءة."""
    sb.table("notifications").update({
        "is_read": True,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", current_user["id"]).eq("is_read", False).execute()
    return {"message": "تم تحديد جميع الإشعارات كمقروءة"}
