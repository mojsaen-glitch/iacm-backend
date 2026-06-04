import logging
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Query, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.websockets.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messages", tags=["Messages"])

# Crew may always message these operational roles.
_MANAGER_ROLES = ["super_admin", "admin", "ops_manager", "scheduler"]


# ══════════════════════════════════════════════════════════════════════════════
#  Participant model
#  ───────────────────────────────────────────────────────────────────────────
#  A message participant is EITHER a USER (user_id) or a CREW member (crew_id).
#  Crew are addressed by crew_id so a crew member can message ALL crew on their
#  flight even when those crew have no `users` login account. When such a crew
#  member later logs in (a user whose crew_id matches), they see the messages
#  addressed to their crew_id.
# ══════════════════════════════════════════════════════════════════════════════

def _crew_flight_mate_ids(current_user: dict, sb) -> set:
    """crew_ids assigned to the SAME flights as the current crew user (excl self)."""
    my = current_user.get("crew_id")
    if not my:
        return set()
    asg = sb.table("assignments").select("flight_id").eq("crew_id", my).execute().data or []
    flights = [a["flight_id"] for a in asg if a.get("flight_id")]
    if not flights:
        return set()
    rows = sb.table("assignments").select("crew_id").in_("flight_id", flights).execute().data or []
    return {r["crew_id"] for r in rows if r.get("crew_id") and r["crew_id"] != my}


def _crew_contacts(current_user: dict, sb) -> list:
    """Contacts for a CREW user: ONLY the crew on their own flights (from `crew`
    DIRECTLY — no login account needed). A crew member messages just the crew
    they fly with — no one else. Each contact carries `type` = 'crew'."""
    out: list = []
    mate_ids = _crew_flight_mate_ids(current_user, sb)
    if mate_ids:
        crows = sb.table("crew").select("id, full_name_ar, full_name_en, rank") \
            .in_("id", list(mate_ids)).execute().data or []
        for c in crows:
            out.append({"type": "crew", "id": c["id"],
                        "name_ar": c.get("full_name_ar", ""),
                        "name_en": c.get("full_name_en", ""),
                        "role": c.get("rank", ""), "email": ""})
    out.sort(key=lambda u: (u.get("name_ar") or u.get("name_en") or ""))
    return out


def _involved_or_filter(me: str, mycrew) -> str:
    """PostgREST or() clause matching every message this user is a party to."""
    parts = [f"sender_id.eq.{me}", f"receiver_id.eq.{me}"]
    if mycrew:
        parts += [f"sender_crew_id.eq.{mycrew}", f"receiver_crew_id.eq.{mycrew}"]
    return ",".join(parts)


# ── List users available to message ──────────────────────────────────────────
@router.get("/users")
async def list_users(current_user: CurrentUser, sb: SbClient):
    """Return messageable contacts.
    - Crew: operational managers (users) + crew on THEIR flights (from `crew`).
    - Staff: all active users in their company.
    Each contact has `type` = 'user' | 'crew'.
    """
    if current_user.get("role") == "crew":
        return _crew_contacts(current_user, sb)

    rows = sb.table("users").select("id, name_ar, name_en, role, email") \
        .eq("company_id", current_user["company_id"]).eq("is_active", True) \
        .neq("id", current_user["id"]).order("name_ar").execute().data or []
    return [{"type": "user", "id": r["id"], "name_ar": r.get("name_ar", ""),
             "name_en": r.get("name_en", ""), "role": r.get("role", ""),
             "email": r.get("email", "")} for r in rows]


# ── Conversations list ────────────────────────────────────────────────────────
@router.get("/conversations")
async def list_conversations(current_user: CurrentUser, sb: SbClient):
    """One entry per conversation partner (user OR crew) with last message +
    unread count."""
    me = current_user["id"]
    mycrew = current_user.get("crew_id")

    messages = sb.table("messages") \
        .select("id, sender_id, receiver_id, sender_crew_id, receiver_crew_id, "
                "content, is_read, created_at") \
        .or_(_involved_or_filter(me, mycrew)) \
        .order("created_at", desc=True).execute().data or []

    def _i_sent(m):
        return m.get("sender_id") == me or (mycrew and m.get("sender_crew_id") == mycrew)

    def _partner(m):
        # (type, id) of the OTHER party in this message.
        if _i_sent(m):
            return ("crew", m["receiver_crew_id"]) if m.get("receiver_crew_id") \
                else ("user", m.get("receiver_id"))
        return ("crew", m["sender_crew_id"]) if m.get("sender_crew_id") \
            else ("user", m.get("sender_id"))

    order: list = []
    seen: set = set()
    last_by: dict = {}
    unread_by: dict = {}
    for m in messages:
        ptype, pid = _partner(m)
        if not pid:
            continue
        key = f"{ptype}:{pid}"
        if key not in seen:
            seen.add(key)
            order.append((ptype, pid))
            last_by[key] = m
        if not _i_sent(m) and not m.get("is_read"):
            unread_by[key] = unread_by.get(key, 0) + 1

    if not order:
        return []

    user_ids = [pid for (t, pid) in order if t == "user"]
    crew_ids = [pid for (t, pid) in order if t == "crew"]
    umap = {}
    if user_ids:
        for u in (sb.table("users").select("id, name_ar, name_en, role")
                  .in_("id", user_ids).execute().data or []):
            umap[u["id"]] = u
    cmap = {}
    if crew_ids:
        for c in (sb.table("crew").select("id, full_name_ar, full_name_en, rank")
                  .in_("id", crew_ids).execute().data or []):
            cmap[c["id"]] = c

    convos = []
    for (ptype, pid) in order:
        key = f"{ptype}:{pid}"
        last = last_by.get(key)
        if ptype == "user":
            info = umap.get(pid, {})
            name_ar, name_en, role = info.get("name_ar", ""), info.get("name_en", ""), info.get("role", "")
        else:
            info = cmap.get(pid, {})
            name_ar, name_en, role = info.get("full_name_ar", ""), info.get("full_name_en", ""), info.get("rank", "")
        convos.append({
            "partner_type": ptype,
            "partner_id": pid,
            "user_id": pid,  # backward-compat key the UI already reads
            "name_ar": name_ar, "name_en": name_en, "role": role,
            "last_message": last["content"] if last else "",
            "last_at": last["created_at"] if last else None,
            "unread": unread_by.get(key, 0),
        })
    return convos


# ── Messages with a specific partner (user OR crew) ───────────────────────────
@router.get("/{partner_id}")
async def get_messages(
    partner_id: str,
    current_user: CurrentUser,
    sb: SbClient,
    partner_type: str = Query("user"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """Paginated messages between the current user and a partner. `partner_type`
    is 'user' (default) or 'crew'."""
    me = current_user["id"]
    mycrew = current_user.get("crew_id")
    skip = (page - 1) * page_size

    if partner_type == "crew":
        clauses = [
            f"and(sender_id.eq.{me},receiver_crew_id.eq.{partner_id})",
            f"and(sender_crew_id.eq.{partner_id},receiver_id.eq.{me})",
        ]
        if mycrew:
            clauses += [
                f"and(sender_crew_id.eq.{mycrew},receiver_crew_id.eq.{partner_id})",
                f"and(sender_crew_id.eq.{partner_id},receiver_crew_id.eq.{mycrew})",
            ]
    else:
        clauses = [
            f"and(sender_id.eq.{me},receiver_id.eq.{partner_id})",
            f"and(sender_id.eq.{partner_id},receiver_id.eq.{me})",
        ]
        if mycrew:
            clauses += [
                f"and(sender_crew_id.eq.{mycrew},receiver_id.eq.{partner_id})",
                f"and(sender_id.eq.{partner_id},receiver_crew_id.eq.{mycrew})",
            ]

    result = sb.table("messages").select("*", count="exact") \
        .or_(",".join(clauses)) \
        .order("created_at", desc=False) \
        .range(skip, skip + page_size - 1).execute()
    return {"items": result.data or [], "total": result.count or 0}


# ── Send a message ────────────────────────────────────────────────────────────
@router.post("")
async def send_message(payload: dict, current_user: CurrentUser, sb: SbClient):
    """Send a message. Body: { content, and ONE of receiver_id | receiver_crew_id }."""
    content = (payload.get("content") or "").strip()
    receiver_id = (payload.get("receiver_id") or "").strip() or None
    receiver_crew_id = (payload.get("receiver_crew_id") or "").strip() or None

    if not content or (not receiver_id and not receiver_crew_id):
        raise HTTPException(status_code=422,
                            detail="content and one of receiver_id/receiver_crew_id are required")

    is_crew = current_user.get("role") == "crew"
    mycrew = current_user.get("crew_id")

    # ── Authorisation ──
    # A crew member may ONLY message the crew on their own flights (receiver_crew_id).
    if is_crew:
        if not receiver_crew_id:
            raise HTTPException(status_code=403, detail="يمكنك مراسلة طاقم رحلاتك فقط")
        if receiver_crew_id not in _crew_flight_mate_ids(current_user, sb):
            raise HTTPException(status_code=403, detail="يمكنك مراسلة طاقم رحلاتك فقط")

    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": str(uuid.uuid4()),
        "sender_id": current_user["id"],
        "sender_crew_id": mycrew,
        "receiver_id": receiver_id,
        "receiver_crew_id": receiver_crew_id,
        "content": content,
        "is_read": False,
        "created_at": now,
    }
    try:
        result = sb.table("messages").insert(row).execute()
    except Exception as e:
        logger.exception("message insert failed")
        raise HTTPException(status_code=502, detail=f"تعذّر إرسال الرسالة: {str(e)[:200]}")
    msg = result.data[0] if result.data else row

    # WebSocket push: notify the recipient user(s). For a crew recipient, notify
    # any user account linked to that crew_id (none → stored until they log in).
    target_user_ids = []
    if receiver_id:
        target_user_ids = [receiver_id]
    elif receiver_crew_id:
        urows = sb.table("users").select("id").eq("crew_id", receiver_crew_id) \
            .eq("is_active", True).execute().data or []
        target_user_ids = [u["id"] for u in urows]
    for uid in target_user_ids:
        try:
            await ws_manager.send_to_user(uid, "new_message", msg)
        except Exception as e:
            logger.warning("WebSocket broadcast to user %s failed: %s", uid, e)

    return msg


# ── Mark conversation as read ─────────────────────────────────────────────────
@router.patch("/{partner_id}/read")
async def mark_conversation_read(
    partner_id: str,
    current_user: CurrentUser,
    sb: SbClient,
    partner_type: str = Query("user"),
):
    """Mark messages FROM the partner TO the current user as read."""
    me = current_user["id"]
    mycrew = current_user.get("crew_id")

    if partner_type == "crew":
        clauses = [f"and(sender_crew_id.eq.{partner_id},receiver_id.eq.{me})"]
        if mycrew:
            clauses.append(f"and(sender_crew_id.eq.{partner_id},receiver_crew_id.eq.{mycrew})")
    else:
        clauses = [f"and(sender_id.eq.{partner_id},receiver_id.eq.{me})"]
        if mycrew:
            clauses.append(f"and(sender_id.eq.{partner_id},receiver_crew_id.eq.{mycrew})")

    rows = sb.table("messages").select("id") \
        .or_(",".join(clauses)).eq("is_read", False).execute().data or []
    ids = [r["id"] for r in rows]
    if ids:
        sb.table("messages").update({"is_read": True}).in_("id", ids).execute()
    return {"message": "Marked as read", "updated": len(ids)}
