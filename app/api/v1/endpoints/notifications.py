import uuid, math, logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Query, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError, NotFoundError
from app.services import push_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["Notifications"])

# Roles allowed to send notifications to others
SENDER_ROLES = {"super_admin", "admin", "ops_manager"}
VALID_PLATFORMS = {"android", "ios", "web", "windows"}

# ── Notification-delivery monitoring ─────────────────────────────────────────
# Roles allowed to view the delivery dashboard for a flight (ops + every
# scheduler tier — they're the ones who need to see if crew got the roster).
DELIVERY_VIEWER_ROLES = SENDER_ROLES | {
    "scheduler", "scheduler_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
}
_DELIVERY_CONFIRM_DEADLINE = timedelta(minutes=3)   # no delivered ACK → not confirmed
_READ_DEADLINE            = timedelta(minutes=10)   # delivered but unread → overdue


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _apply_delivery_timeouts(sb, rows: list) -> list:
    """Lazily transition stale delivery rows (no cron needed — runs when the
    dashboard is queried, mirroring the flights advance-statuses pattern):
      • sent & no delivered_at & >3min  → delivery_not_confirmed
      • delivered & no read_at & >10min → unread_after_deadline
    Persists each transition best-effort and returns the (mutated) rows."""
    now = datetime.now(timezone.utc)
    for r in rows:
        new_status = None
        st = r.get("status")
        if st == "sent" and not r.get("delivered_at"):
            sent = _parse_ts(r.get("sent_at"))
            if sent and (now - sent) > _DELIVERY_CONFIRM_DEADLINE:
                new_status = "delivery_not_confirmed"
        elif st == "delivered" and not r.get("read_at"):
            dlv = _parse_ts(r.get("delivered_at"))
            if dlv and (now - dlv) > _READ_DEADLINE:
                new_status = "unread_after_deadline"
        if new_status:
            r["status"] = new_status
            try:
                sb.table("notification_delivery").update(
                    {"status": new_status, "updated_at": now.isoformat()}
                ).eq("id", r["id"]).execute()
            except Exception as e:
                log.warning("delivery timeout update failed (%s): %s", r.get("id"), e)
    return rows


# ─── GET /notifications ──────────────────────────────────────────
@router.get("")
async def list_notifications(
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    unread_only: bool = False,
    type: str | None = None,
):
    query = sb.table("notifications").select("*", count="exact").eq("user_id", current_user["id"])
    if unread_only:
        query = query.eq("is_read", False)
    if type:
        query = query.eq("type", type)
    skip = (page - 1) * page_size
    result = query.order("created_at", desc=True).range(skip, skip + page_size - 1).execute()
    total = result.count or 0
    return {
        "items": result.data,
        "total": total,
        "page": page,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
    }


# ─── POST /notifications — send (+ push) ─────────────────────────
@router.post("", status_code=201)
async def send_notification(data: dict, current_user: CurrentUser, sb: SbClient):
    """إرسال إشعار لمستخدم أو لجميع مستخدمي الشركة. الأدمن والمدير فقط.

    يقوم تلقائياً بإرسال Push Notification إلى الأجهزة المسجّلة (FCM)
    بالتوازي مع كتابة الإشعار في DB. الفشل بالـ push لا يُوقف العملية.
    """
    if current_user["role"] not in SENDER_ROLES:
        raise ForbiddenError("فقط الإدمن ومدير العمليات يمكنهم إرسال الإشعارات")

    company_id     = current_user["company_id"]
    target_user_id = data.get("user_id")
    send_to_all    = data.get("send_to_all", False)

    title_ar  = data.get("title_ar", "")
    title_en  = data.get("title_en", "")
    message_ar = data.get("message_ar", "")
    message_en = data.get("message_en", "")
    notif_type = data.get("type", "manual")
    reference_id   = data.get("reference_id")
    reference_type = data.get("reference_type")

    now = datetime.now(timezone.utc).isoformat()

    if send_to_all:
        users_res = sb.table("users").select("id").eq("company_id", company_id).eq("is_active", True).execute()
        target_ids = [u["id"] for u in (users_res.data or [])]
    elif target_user_id:
        user_check = sb.table("users").select("id").eq("id", target_user_id).eq("company_id", company_id).execute()
        if not user_check.data:
            raise NotFoundError("User", target_user_id)
        target_ids = [target_user_id]
    else:
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

    if notifs:
        sb.table("notifications").insert(notifs).execute()

    # Best-effort push — never blocks the API response
    push_result = {"attempted": 0, "succeeded": 0, "failed": 0, "stub": True}
    try:
        push_result = push_service.send_to_users(
            sb,
            target_ids,
            title=title_ar or title_en or "إشعار",
            body=message_ar or message_en or "",
            data={
                "type":            notif_type,
                "reference_id":    str(reference_id) if reference_id else "",
                "reference_type":  reference_type or "",
            },
        )
    except Exception as e:
        log.warning("send_notification: push delivery failed — %s", e)

    return {
        "sent":  len(notifs),
        "push":  push_result,
        "message": "تم إرسال الإشعارات بنجاح",
    }


# ─── POST /notifications/{id}/read ───────────────────────────────
@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, current_user: CurrentUser, sb: SbClient):
    """يسمح فقط لصاحب الإشعار بتحديده كمقروء. يحدّث سجل التوصيل أيضاً (READ ACK)."""
    now = datetime.now(timezone.utc).isoformat()
    result = sb.table("notifications").update({
        "is_read": True,
        "read_at": now,
    }).eq("id", notification_id).eq("user_id", current_user["id"]).execute()

    if not result.data:
        raise NotFoundError("Notification", notification_id)

    # Delivery monitoring: reading implies delivery — stamp both if unset.
    # Best-effort: a missing notification_delivery table must never break read.
    try:
        sb.table("notification_delivery").update({
            "status": "read", "read_at": now, "updated_at": now,
        }).eq("notification_id", notification_id) \
          .eq("user_id", current_user["id"]).is_("read_at", "null").execute()
    except Exception as e:
        log.warning("mark_read delivery update failed (%s): %s", notification_id, e)
    return {"message": "تم تحديد الإشعار كمقروء"}


@router.post("/{notification_id}/ack-delivered")
async def ack_delivered(notification_id: str, current_user: CurrentUser, sb: SbClient):
    """تطبيق الطاقم يستدعيه فور استلام الإشعار (DELIVERED ACK). يضبط delivered_at
    وحالة delivered للمرة الأولى فقط (idempotent)."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("notification_delivery").update({
            "status": "delivered", "delivered_at": now, "updated_at": now,
        }).eq("notification_id", notification_id) \
          .eq("user_id", current_user["id"]) \
          .is_("delivered_at", "null") \
          .in_("status", ["sent", "failed", "delivery_not_confirmed"]).execute()
    except Exception as e:
        log.warning("ack_delivered failed (%s): %s", notification_id, e)
    return {"ok": True}


@router.get("/delivery")
async def delivery_status(
    current_user: CurrentUser,
    sb: SbClient,
    flight_id: str = Query(..., min_length=1),
):
    """لوحة الجدولة: حالة توصيل إشعارات تكليف الطاقم لرحلة معيّنة، لكل فرد.
    يطبّق التحويلات الزمنية (3د/10د) lazily قبل الإرجاع."""
    if current_user["role"] not in DELIVERY_VIEWER_ROLES:
        raise ForbiddenError("عرض حالة التوصيل متاح للجدولة/الإدارة فقط")
    rows = sb.table("notification_delivery").select("*") \
        .eq("flight_id", flight_id).order("sent_at", desc=True).execute().data or []
    rows = _apply_delivery_timeouts(sb, rows)
    # Attach crew display name/roster for the dashboard.
    crew_ids = list({r["crew_id"] for r in rows if r.get("crew_id")})
    names: dict = {}
    if crew_ids:
        cres = sb.table("crew").select("id,full_name_ar,full_name_en,roster_name") \
            .in_("id", crew_ids).execute().data or []
        names = {c["id"]: c for c in cres}
    for r in rows:
        c = names.get(r.get("crew_id")) or {}
        r["crew_name"]   = c.get("full_name_ar") or c.get("full_name_en")
        r["roster_name"] = c.get("roster_name")
    return rows


# ─── GET /notifications/unread/count ─────────────────────────────
@router.get("/unread/count")
async def get_unread_count(current_user: CurrentUser, sb: SbClient):
    """Returns count of unread notifications for the current user."""
    result = sb.table("notifications").select("id", count="exact") \
        .eq("user_id", current_user["id"]).eq("is_read", False).execute()
    return {"count": result.count or 0}


# ─── POST /notifications/read-all ────────────────────────────────
@router.post("/read-all")
async def mark_all_read(current_user: CurrentUser, sb: SbClient):
    """تحديد جميع إشعارات المستخدم الحالي كمقروءة."""
    sb.table("notifications").update({
        "is_read": True,
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", current_user["id"]).eq("is_read", False).execute()
    return {"message": "تم تحديد جميع الإشعارات كمقروءة"}


# ─── DELETE /notifications/{id} ──────────────────────────────────
@router.delete("/{notification_id}", status_code=204)
async def delete_notification(notification_id: str, current_user: CurrentUser, sb: SbClient):
    """يحذف صاحب الإشعار إشعاره فقط — الأمان مفروض على user_id."""
    res = sb.table("notifications").delete() \
        .eq("id", notification_id).eq("user_id", current_user["id"]).execute()
    if not res.data:
        raise NotFoundError("Notification", notification_id)


# ─── POST /notifications/clear-all ───────────────────────────────
@router.post("/clear-all")
async def clear_all(current_user: CurrentUser, sb: SbClient):
    """يحذف جميع الإشعارات المقروءة فقط — يحتفظ بغير المقروءة كأمان."""
    sb.table("notifications").delete() \
        .eq("user_id", current_user["id"]).eq("is_read", True).execute()
    return {"message": "تم حذف جميع الإشعارات المقروءة"}


# ─── POST /notifications/register-device ─────────────────────────
@router.post("/register-device", status_code=201)
async def register_device(data: dict, current_user: CurrentUser, sb: SbClient):
    """يسجّل جهاز المستخدم لتلقّي Push Notifications.

    Body: { token, platform, app_version?, device_name? }
    Idempotent: التوكن المكرر يُحدّث last_seen_at بدل ما يفشل.
    """
    token = (data.get("token") or "").strip()
    platform = (data.get("platform") or "").strip().lower()
    if not token:
        raise HTTPException(status_code=422, detail="token مطلوب")
    if platform not in VALID_PLATFORMS:
        raise HTTPException(
            status_code=422,
            detail=f"platform يجب أن يكون أحد: {', '.join(sorted(VALID_PLATFORMS))}",
        )

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "user_id":       current_user["id"],
        "token":         token,
        "platform":      platform,
        "app_version":   data.get("app_version"),
        "device_name":   data.get("device_name"),
        "last_seen_at":  now,
    }

    # Upsert on the unique token — same device, fresh registration ⇒ refresh.
    try:
        res = sb.table("device_tokens").upsert(payload, on_conflict="token").execute()
        return {"registered": True, "id": res.data[0]["id"] if res.data else None}
    except Exception as e:
        log.exception("register_device failed: %s", e)
        raise HTTPException(status_code=502, detail=f"تعذّر تسجيل الجهاز: {str(e)[:200]}")


# ─── DELETE /notifications/unregister-device ─────────────────────
@router.delete("/unregister-device", status_code=204)
async def unregister_device(token: str = Query(..., min_length=8), *, current_user: CurrentUser, sb: SbClient):
    """يحذف توكن الجهاز عند تسجيل الخروج — يضمن إنه ما يصير ينام."""
    sb.table("device_tokens").delete() \
        .eq("token", token).eq("user_id", current_user["id"]).execute()
