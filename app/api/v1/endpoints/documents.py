import uuid, os, math, logging
from datetime import date, timedelta, datetime, timezone
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ForbiddenError
from app.core.config import settings
from app.services import push_service

router = APIRouter(prefix="/documents", tags=["Documents"])
log = logging.getLogger(__name__)


def _verify_doc_in_company(sb, doc_id: str, company_id: str) -> dict | None:
    """Fetch the document only if its crew belongs to the caller's company.
    Returns the document row (with crew_id) or None. Uses a single inner-joined
    query so it cannot be bypassed via TOCTOU on a separate verification call."""
    res = sb.table("documents") \
        .select("id, crew_id, crew!inner(company_id)") \
        .eq("id", doc_id) \
        .eq("crew.company_id", company_id) \
        .execute()
    return res.data[0] if res.data else None


@router.get("/crew/{crew_id}")
async def get_crew_documents(crew_id: str, current_user: CurrentUser, sb: SbClient):
    crew_check = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)
    result = sb.table("documents").select("*").eq("crew_id", crew_id).execute()
    today = date.today()
    warning = today + timedelta(days=30)
    docs = []
    for doc in (result.data or []):
        status = "valid"
        if doc.get("expiry_date"):
            exp = date.fromisoformat(doc["expiry_date"])
            if exp < today:
                status = "expired"
            elif exp <= warning:
                status = "expiring"
        doc["status"] = status
        docs.append(doc)
    return docs


@router.post("", status_code=201)
async def create_document(data: dict, current_user: CurrentUser, sb: SbClient):
    crew_id_in = data.get("crew_id")
    if not crew_id_in:
        raise NotFoundError("Crew member", "missing")
    crew = sb.table("crew").select("id").eq("id", crew_id_in).eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Crew member", crew_id_in)

    # Normalise legacy front-end field names to the real DB columns. Older
    # mobile/desktop builds POST `doc_type`/`doc_number`/`issuing_country`
    # which Supabase rejects with HTTP 500 because those columns don't exist.
    # We accept both forms and drop the aliases so the insert succeeds.
    alias_map = {
        "doc_type":        "document_type",
        "doc_number":      "document_number",
        "issuing_country": "issued_by",
    }
    for legacy, real in alias_map.items():
        if legacy in data:
            # Only fill the real column if it wasn't already provided directly.
            data.setdefault(real, data[legacy])
            data.pop(legacy)

    # Whitelist columns we know exist on `documents`. Anything else (e.g. a
    # stray field from a future client) is dropped instead of triggering a
    # PostgREST 400 / 500.
    allowed = {
        "crew_id", "document_type", "document_number", "issue_date", "expiry_date",
        "issued_by", "file_path", "notes",
    }
    data = {k: v for k, v in data.items() if k in allowed}

    data["id"] = str(uuid.uuid4())
    data["is_verified"] = False
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("documents").insert(data).execute()
    return result.data[0] if result.data else {}


@router.patch("/{doc_id}")
async def update_document(doc_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Update a document (e.g. expiry date, doc number).

    Documents drive compliance (expiry → grounding). Mutation is restricted
    to admin / ops_manager / compliance_officer so a crew member can't
    extend their own medical certificate.
    """
    if current_user["role"] not in {"super_admin", "admin", "ops_manager", "compliance_officer"}:
        raise ForbiddenError("غير مصرح بتعديل الوثائق")
    if not _verify_doc_in_company(sb, doc_id, current_user["company_id"]):
        raise NotFoundError("Document", doc_id)
    # Strip caller-controlled fields that must not be patched
    for forbidden in ("id", "crew_id", "company_id", "created_at"):
        data.pop(forbidden, None)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("documents").update(data).eq("id", doc_id).execute()
    return result.data[0] if result.data else {}


@router.delete("/{doc_id}", status_code=204)
async def delete_document(doc_id: str, current_user: CurrentUser, sb: SbClient):
    """Delete a document. Admin / Ops Manager only."""
    if current_user["role"] not in ("super_admin", "admin", "ops_manager"):
        raise ForbiddenError("Insufficient permissions")

    doc = _verify_doc_in_company(sb, doc_id, current_user["company_id"])
    if not doc:
        raise NotFoundError("Document", doc_id)

    sb.table("documents").delete().eq("id", doc_id).execute()

    try:
        sb.table("audit_log").insert({
            "user_id":     current_user["id"],
            "user_name":   current_user.get("name_ar") or current_user.get("name_en") or current_user["email"],
            "action":      "delete_document",
            "entity_type": "document",
            "entity_id":   doc_id,
            "company_id":  current_user["company_id"],
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        log.exception("Failed to write audit log for document delete")


@router.get("/expiring")
async def get_expiring_documents(
    current_user: CurrentUser,
    sb: SbClient,
    days: int = Query(30, ge=1, le=90),
):
    today = date.today().isoformat()
    warning = (date.today() + timedelta(days=days)).isoformat()
    result = sb.table("documents").select("*, crew!inner(company_id, full_name_ar, full_name_en)")\
        .eq("crew.company_id", current_user["company_id"])\
        .lte("expiry_date", warning).gte("expiry_date", today)\
        .order("expiry_date").execute()
    return result.data or []


# ─── POST /documents/check-expiring-reminders ────────────────────
# Scans every document in the caller's company that falls inside the
# 30-day expiry window and creates a notification (+ push) for the crew
# member who owns it. Idempotent: a doc won't be re-nagged within 7 days
# of its `last_reminder_sent` so admins can call this on every login or
# wire it to a Vercel cron without spamming users.
_REMINDER_COOLDOWN_DAYS = 7
_REMINDER_WINDOW_DAYS   = 30

# Friendly label map for the notification message — keeps backend
# self-contained without pulling translations from the frontend.
_DOC_LABELS_AR: dict[str, str] = {
    "passport":            "جواز السفر",
    "medical_certificate": "الفحص الطبي",
    "medical":             "الفحص الطبي",
    "license":             "الإجازة",
    "crew_id":             "بطاقة الطاقم",
    "safety":              "شهادة السلامة",
    "emergency":           "شهادة الطوارئ",
}


@router.post("/check-expiring-reminders", status_code=200)
async def check_expiring_reminders(current_user: CurrentUser, sb: SbClient):
    """Send a one-month-out reminder for every expiring document in the
    company. Admins and ops managers only — crew members can still SEE their
    own reminders via the regular notifications feed."""
    if current_user["role"] not in {"admin", "ops_manager"}:
        raise ForbiddenError("الإدمن ومدير العمليات فقط")

    company_id = current_user["company_id"]
    now        = datetime.now(timezone.utc)
    today      = now.date()
    cutoff     = (today + timedelta(days=_REMINDER_WINDOW_DAYS)).isoformat()
    cooldown   = (now - timedelta(days=_REMINDER_COOLDOWN_DAYS)).isoformat()

    # 1. Docs expiring in [today, today+30] for this company.
    docs_res = sb.table("documents")\
        .select("*, crew!inner(id, company_id, full_name_ar, full_name_en)")\
        .eq("crew.company_id", company_id)\
        .lte("expiry_date", cutoff)\
        .gte("expiry_date", today.isoformat())\
        .execute()
    docs = docs_res.data or []

    if not docs:
        return {"sent": 0, "skipped": 0, "checked": 0}

    # 2. Resolve crew → user mapping in ONE query instead of N.
    crew_ids = list({d["crew_id"] for d in docs})
    users_res = sb.table("users").select("id, crew_id")\
        .in_("crew_id", crew_ids).eq("is_active", True).execute()
    crew_to_user = {u["crew_id"]: u["id"] for u in (users_res.data or [])}

    sent    = 0
    skipped = 0
    notifs  = []
    updates = []     # (doc_id, ) for updating last_reminder_sent
    push_targets: list[tuple[str, str, str]] = []  # (user_id, title, body)

    for doc in docs:
        last = doc.get("last_reminder_sent")
        if last and last > cooldown:
            skipped += 1
            continue

        user_id = crew_to_user.get(doc["crew_id"])
        if not user_id:
            # Crew has no login account yet → no one to notify
            skipped += 1
            continue

        try:
            expiry = date.fromisoformat(doc["expiry_date"])
        except (ValueError, TypeError):
            skipped += 1
            continue

        days_left = (expiry - today).days
        doc_type  = doc.get("document_type", "")
        label_ar  = _DOC_LABELS_AR.get(doc_type, doc_type)

        title_ar = "تنبيه: وثيقتك ستنتهي قريباً"
        title_en = "Document Expiring Soon"
        if days_left <= 0:
            msg_ar = f"وثيقتك ({label_ar}) منتهية الصلاحية"
            msg_en = f"Your {doc_type} has expired"
        elif days_left == 1:
            msg_ar = f"وثيقتك ({label_ar}) ستنتهي غداً"
            msg_en = f"Your {doc_type} expires tomorrow"
        else:
            msg_ar = f"وثيقتك ({label_ar}) ستنتهي خلال {days_left} يوماً"
            msg_en = f"Your {doc_type} expires in {days_left} days"

        notifs.append({
            "id":             str(uuid.uuid4()),
            "user_id":        user_id,
            "type":           "document_expiring",
            "title_ar":       title_ar,
            "title_en":       title_en,
            "message_ar":     msg_ar,
            "message_en":     msg_en,
            "reference_id":   doc["id"],
            "reference_type": "document",
            "is_read":        False,
            "created_at":     now.isoformat(),
        })
        updates.append(doc["id"])
        push_targets.append((user_id, title_ar, msg_ar))
        sent += 1

    if notifs:
        sb.table("notifications").insert(notifs).execute()
        # Stamp last_reminder_sent so we don't double-notify within 7 days.
        for doc_id in updates:
            sb.table("documents").update({
                "last_reminder_sent": now.isoformat(),
            }).eq("id", doc_id).execute()

        # Best-effort push — one call per user. Any failure is logged
        # but doesn't fail the request because the DB notification is the
        # source of truth (polling will still surface it).
        for user_id, title, body in push_targets:
            try:
                push_service.send_to_users(sb, [user_id],
                    title=title, body=body,
                    data={"type": "document_expiring"})
            except Exception as e:
                log.warning("doc-expiry push failed for %s: %s", user_id, e)

    return {"sent": sent, "skipped": skipped, "checked": len(docs)}
