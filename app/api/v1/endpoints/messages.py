from datetime import datetime, timezone  # used in send_message
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser

router = APIRouter(prefix="/messages", tags=["Messages"])


# ── List users available to message ──────────────────────────────────────────
@router.get("/users")
async def list_users(current_user: CurrentUser, sb: SbClient):
    """Return messageable users.
    - Crew members: only see admin/ops_manager/scheduler/super_admin in their company.
    - Staff: see all active users in their company.
    """
    query = sb.table("users") \
        .select("id, name_ar, name_en, role, email") \
        .eq("company_id", current_user["company_id"]) \
        .eq("is_active", True) \
        .neq("id", current_user["id"])

    # Crew can only message supervisors/managers — not other crew members
    if current_user.get("role") == "crew":
        query = query.in_("role", ["super_admin", "admin", "ops_manager", "scheduler"])

    result = query.order("name_ar").execute()
    return result.data


# ── Conversations list ────────────────────────────────────────────────────────
@router.get("/conversations")
async def list_conversations(current_user: CurrentUser, sb: SbClient):
    """
    Return one entry per conversation partner with:
    - last message content + timestamp
    - unread count (messages sent TO current_user that are unread)
    """
    me = current_user["id"]

    # All messages involving current user
    result = sb.table("messages") \
        .select("id, sender_id, receiver_id, content, is_read, created_at") \
        .or_(f"sender_id.eq.{me},receiver_id.eq.{me}") \
        .order("created_at", desc=True) \
        .execute()

    messages = result.data or []

    # Collect unique partner IDs
    partner_ids: list[str] = []
    seen: set[str] = set()
    for m in messages:
        partner = m["receiver_id"] if m["sender_id"] == me else m["sender_id"]
        if partner not in seen:
            seen.add(partner)
            partner_ids.append(partner)

    if not partner_ids:
        return []

    # Fetch partner user details
    users_result = sb.table("users") \
        .select("id, name_ar, name_en, role") \
        .in_("id", partner_ids) \
        .execute()
    users_map = {u["id"]: u for u in (users_result.data or [])}

    # Build conversation summaries
    convos = []
    for pid in partner_ids:
        # Last message between me and this partner
        last_msg = next(
            (m for m in messages
             if m["sender_id"] in (me, pid) and m["receiver_id"] in (me, pid)),
            None,
        )
        # Unread count: messages FROM partner TO me that are unread
        unread = sum(
            1 for m in messages
            if m["sender_id"] == pid and m["receiver_id"] == me and not m["is_read"]
        )
        user_info = users_map.get(pid, {})
        convos.append({
            "user_id": pid,
            "name_ar": user_info.get("name_ar", ""),
            "name_en": user_info.get("name_en", ""),
            "role": user_info.get("role", ""),
            "last_message": last_msg["content"] if last_msg else "",
            "last_at": last_msg["created_at"] if last_msg else None,
            "unread": unread,
        })

    return convos


# ── Messages with a specific user ─────────────────────────────────────────────
@router.get("/{other_user_id}")
async def get_messages(
    other_user_id: str,
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """Return paginated messages between current user and other_user_id."""
    me = current_user["id"]
    skip = (page - 1) * page_size

    result = sb.table("messages") \
        .select("*", count="exact") \
        .or_(
            f"and(sender_id.eq.{me},receiver_id.eq.{other_user_id}),"
            f"and(sender_id.eq.{other_user_id},receiver_id.eq.{me})"
        ) \
        .order("created_at", desc=False) \
        .range(skip, skip + page_size - 1) \
        .execute()

    return {
        "items": result.data or [],
        "total": result.count or 0,
    }


# ── Send a message ────────────────────────────────────────────────────────────
@router.post("")
async def send_message(
    payload: dict,
    current_user: CurrentUser,
    sb: SbClient,
):
    """Send a message. Body: { receiver_id, content }"""
    receiver_id = payload.get("receiver_id", "").strip()
    content = payload.get("content", "").strip()

    if not receiver_id or not content:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="receiver_id and content are required")

    now = datetime.now(timezone.utc).isoformat()
    result = sb.table("messages").insert({
        "sender_id": current_user["id"],
        "receiver_id": receiver_id,
        "content": content,
        "is_read": False,
        "created_at": now,
    }).execute()

    return result.data[0] if result.data else {}


# ── Mark conversation as read ─────────────────────────────────────────────────
@router.patch("/{other_user_id}/read")
async def mark_conversation_read(
    other_user_id: str,
    current_user: CurrentUser,
    sb: SbClient,
):
    """Mark all messages FROM other_user TO current_user as read."""
    sb.table("messages").update({
        "is_read": True,
    }) \
        .eq("sender_id", other_user_id) \
        .eq("receiver_id", current_user["id"]) \
        .eq("is_read", False) \
        .execute()
    return {"message": "Marked as read"}
