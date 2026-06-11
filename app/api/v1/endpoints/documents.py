import uuid, json, logging
from datetime import date, timedelta, datetime, timezone
from fastapi import APIRouter, Query, Header, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ForbiddenError
from app.core.config import settings
from app.services import push_service

router = APIRouter(prefix="/documents", tags=["Documents"])
log = logging.getLogger(__name__)

# ── Governance ────────────────────────────────────────────────────────────────
# Only these roles may create / edit / verify / delete crew documents (documents
# drive compliance grounding, so a crew member must not self-manage them).
_DOC_EDITOR_ROLES = {"super_admin", "admin", "ops_manager", "compliance_officer"}

# Fixed document-type vocabulary (NO free text). UI dropdown must match this.
_DOC_TYPES = {
    "passport", "medical_certificate", "crew_license", "cabin_crew_certificate",
    "safety_training", "emergency_training", "first_aid", "crm",
    "dangerous_goods", "visa", "airport_pass", "other",
}
# Types that MUST carry a document_number.
_DOC_NUMBER_REQUIRED = {
    "passport", "medical_certificate", "crew_license",
    "cabin_crew_certificate", "visa", "airport_pass",
}

_REMINDER_COOLDOWN_DAYS = 7
_REMINDER_WINDOW_DAYS   = 30

_DOC_LABELS_AR: dict[str, str] = {
    "passport": "جواز السفر", "medical_certificate": "الفحص الطبي",
    "medical": "الفحص الطبي", "crew_license": "رخصة الطاقم", "license": "الإجازة",
    "cabin_crew_certificate": "شهادة طاقم الضيافة", "safety_training": "تدريب السلامة",
    "emergency_training": "تدريب الطوارئ", "first_aid": "الإسعافات الأولية",
    "crm": "إدارة موارد الطاقم (CRM)", "dangerous_goods": "البضائع الخطرة",
    "visa": "التأشيرة", "airport_pass": "تصريح المطار", "crew_id": "بطاقة الطاقم",
    "other": "وثيقة أخرى",
}


def _ensure_doc_editor(user: dict) -> None:
    if user.get("role") not in _DOC_EDITOR_ROLES and not user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بإدارة الوثائق — مسؤول/مدير عمليات/مسؤول امتثال فقط")


def _verify_doc_in_company(sb, doc_id: str, company_id: str) -> dict | None:
    """Fetch the document only if its crew belongs to the caller's company.
    Single inner-joined query → no TOCTOU bypass."""
    res = sb.table("documents") \
        .select("id, crew_id, crew!inner(company_id)") \
        .eq("id", doc_id).eq("crew.company_id", company_id).execute()
    return res.data[0] if res.data else None


def _parse_date_strict(value, field: str):
    """Return a date, or raise 422 on a malformed value. None/'' → None."""
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        raise HTTPException(status_code=422,
                            detail=f"تاريخ غير صالح في الحقل {field} — استخدم الصيغة YYYY-MM-DD")


def _clean_and_validate(data: dict, *, existing: dict | None = None) -> dict:
    """Validate + normalise a document payload. `existing` is the current row for
    PATCH (so cross-field checks use the merged view). Returns DB-ready columns."""
    # Normalise legacy / aliased field names to the real DB columns.
    aliases = {"doc_type": "document_type", "doc_number": "document_number",
               "issuing_country": "issued_by", "issuing_authority": "issued_by"}
    for legacy, real in aliases.items():
        if legacy in data and data.get(legacy) is not None:
            data.setdefault(real, data[legacy])
        data.pop(legacy, None)

    creating = existing is None
    merged = {**(existing or {}), **data}

    # document_type — required (on create) and from the fixed vocabulary.
    if "document_type" in data or creating:
        dtype = str(data.get("document_type") or merged.get("document_type") or "").strip()
        if not dtype:
            raise HTTPException(status_code=422, detail="نوع الوثيقة مطلوب")
        if dtype not in _DOC_TYPES:
            raise HTTPException(status_code=422,
                                detail=f"نوع وثيقة غير معروف: {dtype}")
        data["document_type"] = dtype

    final_type = merged.get("document_type")

    # 'other' must carry a description in notes.
    if final_type == "other":
        notes = str(data.get("notes", merged.get("notes")) or "").strip()
        if not notes:
            raise HTTPException(status_code=422, detail="النوع 'أخرى' يتطلّب وصفاً في الملاحظات")

    # issuing authority (issued_by) — required.
    if creating or "issued_by" in data:
        if not str(data.get("issued_by", merged.get("issued_by")) or "").strip():
            raise HTTPException(status_code=422, detail="جهة الإصدار مطلوبة")

    # document_number — required for important types.
    if final_type in _DOC_NUMBER_REQUIRED:
        if not str(data.get("document_number", merged.get("document_number")) or "").strip():
            raise HTTPException(status_code=422, detail="رقم الوثيقة مطلوب لهذا النوع")

    # Dates — strict parse; expiry not before issue.
    issue = _parse_date_strict(data.get("issue_date", merged.get("issue_date")), "issue_date")
    expiry = _parse_date_strict(data.get("expiry_date", merged.get("expiry_date")), "expiry_date")
    if creating and issue is None:
        raise HTTPException(status_code=422, detail="تاريخ الإصدار مطلوب")
    if creating and expiry is None:
        raise HTTPException(status_code=422, detail="تاريخ الانتهاء مطلوب")
    if issue and expiry and expiry < issue:
        raise HTTPException(status_code=422,
                            detail="تاريخ الانتهاء لا يمكن أن يسبق تاريخ الإصدار")

    allowed = {"crew_id", "document_type", "document_number", "issue_date",
               "expiry_date", "issued_by", "notes"}
    return {k: v for k, v in data.items() if k in allowed}


def _audit(sb, user, action, doc_id, crew_id, company_id, *, changes: dict | None = None):
    entry = {
        "user_id": user["id"],
        "user_name": user.get("name_ar") or user.get("name_en") or user.get("email", ""),
        "action": action, "entity_type": "document", "entity_id": doc_id,
        "company_id": company_id, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = {"crew_id": crew_id}
    if changes:
        payload["changes"] = changes
    entry["after_data"] = json.dumps(payload, ensure_ascii=False)
    try:
        sb.table("audit_log").insert(entry).execute()
    except Exception:
        log.exception("audit_log insert failed for %s", action)


def _diff(before: dict, after: dict) -> dict:
    keys = ("document_type", "document_number", "issued_by", "issue_date",
            "expiry_date", "notes", "is_verified")
    out = {}
    for k in keys:
        if before.get(k) != after.get(k):
            out[k] = {"old": before.get(k), "new": after.get(k)}
    return out


def _decorate(doc: dict) -> dict:
    """Add expiry_status (valid/expiring/expired/incomplete) + verification flag +
    issuing_authority alias. NEVER hides incomplete rows."""
    today = date.today()
    exp_raw = doc.get("expiry_date")
    status = "valid"
    if not exp_raw:
        status = "incomplete"
    else:
        try:
            exp = date.fromisoformat(str(exp_raw)[:10])
            if exp < today:
                status = "expired"
            elif exp <= today + timedelta(days=_REMINDER_WINDOW_DAYS):
                status = "expiring"
        except (ValueError, TypeError):
            status = "incomplete"   # malformed date is surfaced, never ignored
    if not doc.get("issue_date"):
        status = "incomplete" if status == "valid" else status
    doc["expiry_status"] = status
    doc["status"] = status                                  # legacy key
    doc["is_verified"] = bool(doc.get("is_verified"))
    doc["issuing_authority"] = doc.get("issued_by")
    return doc


# ── Reads ───────────────────────────────────────────────────────────────────
# Documents carry PII (passport / licence numbers). Read access:
#   • editor roles (super_admin/admin/ops_manager/compliance_officer) → ANY crew
#     in their OWN company.
#   • a crew user → ONLY their own documents (current_user.crew_id == crew_id).
#   • everyone else (viewer, schedulers, …) → 403.
# Cross-company reads return 404 (the crew "doesn't exist" in your company).
@router.get("/crew/{crew_id}")
async def get_crew_documents(crew_id: str, current_user: CurrentUser, sb: SbClient):
    is_editor = (current_user.get("role") in _DOC_EDITOR_ROLES
                 or current_user.get("is_superuser"))
    if not is_editor:
        own_crew = current_user.get("crew_id")
        if not (current_user.get("role") == "crew" and own_crew and own_crew == crew_id):
            raise ForbiddenError("غير مصرح بقراءة وثائق هذا الطاقم")

    crew_check = sb.table("crew").select("id").eq("id", crew_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)
    rows = (sb.table("documents").select("*").eq("crew_id", crew_id).execute().data) or []
    return [_decorate(d) for d in rows]


# ── Create ──────────────────────────────────────────────────────────────────
@router.post("", status_code=201)
async def create_document(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_doc_editor(current_user)
    crew_id_in = data.get("crew_id")
    if not crew_id_in:
        raise HTTPException(status_code=422, detail="معرّف الطاقم مطلوب")
    crew = sb.table("crew").select("id").eq("id", crew_id_in) \
        .eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Crew member", crew_id_in)

    clean = _clean_and_validate(data)
    clean["crew_id"] = crew_id_in
    clean["id"] = str(uuid.uuid4())
    clean["is_verified"] = False
    now = datetime.now(timezone.utc).isoformat()
    clean["created_at"] = now
    clean["updated_at"] = now
    result = sb.table("documents").insert(clean).execute()
    row = result.data[0] if result.data else clean

    _audit(sb, current_user, "create_document", row["id"], crew_id_in,
           current_user["company_id"],
           changes={"document_type": {"old": None, "new": clean.get("document_type")},
                    "expiry_date": {"old": None, "new": clean.get("expiry_date")}})
    return _decorate(row)


# ── Update ──────────────────────────────────────────────────────────────────
@router.patch("/{doc_id}")
async def update_document(doc_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Update a document (e.g. correct an expiry date). Editor roles only —
    a crew member must not extend their own medical certificate."""
    _ensure_doc_editor(current_user)
    if not _verify_doc_in_company(sb, doc_id, current_user["company_id"]):
        raise NotFoundError("Document", doc_id)

    before = (sb.table("documents").select("*").eq("id", doc_id).execute().data or [{}])[0]
    for forbidden in ("id", "crew_id", "company_id", "created_at",
                      "is_verified", "verified_by", "verified_at"):
        data.pop(forbidden, None)
    clean = _clean_and_validate(data, existing=before)
    clean["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("documents").update(clean).eq("id", doc_id).execute()
    after = result.data[0] if result.data else {**before, **clean}

    _audit(sb, current_user, "update_document", doc_id, before.get("crew_id"),
           current_user["company_id"], changes=_diff(before, after))
    return _decorate(after)


# ── Verify ──────────────────────────────────────────────────────────────────
@router.post("/{doc_id}/verify")
async def verify_document(doc_id: str, current_user: CurrentUser, sb: SbClient,
                          data: dict | None = None):
    """Mark a document verified (or un-verify with {"verified": false}). Editor
    roles only. Unverified documents are treated as REVIEW by compliance, never
    silently valid."""
    _ensure_doc_editor(current_user)
    if not _verify_doc_in_company(sb, doc_id, current_user["company_id"]):
        raise NotFoundError("Document", doc_id)
    before = (sb.table("documents").select("*").eq("id", doc_id).execute().data or [{}])[0]

    verified = True if data is None else bool(data.get("verified", True))
    now = datetime.now(timezone.utc).isoformat()
    patch = {
        "is_verified": verified,
        "verified_by": current_user["id"] if verified else None,
        "verified_at": now if verified else None,
        "updated_at": now,
    }
    result = sb.table("documents").update(patch).eq("id", doc_id).execute()
    after = result.data[0] if result.data else {**before, **patch}

    _audit(sb, current_user, "verify_document", doc_id, before.get("crew_id"),
           current_user["company_id"],
           changes={"is_verified": {"old": bool(before.get("is_verified")), "new": verified}})
    return _decorate(after)


# ── Delete ──────────────────────────────────────────────────────────────────
@router.delete("/{doc_id}", status_code=204)
async def delete_document(doc_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_doc_editor(current_user)
    doc = _verify_doc_in_company(sb, doc_id, current_user["company_id"])
    if not doc:
        raise NotFoundError("Document", doc_id)
    sb.table("documents").delete().eq("id", doc_id).execute()
    _audit(sb, current_user, "delete_document", doc_id, doc.get("crew_id"),
           current_user["company_id"])


# ── Expiry report ─────────────────────────────────────────────────────────────
@router.get("/expiring")
async def get_expiring_documents(current_user: CurrentUser, sb: SbClient,
                                 days: int = Query(30, ge=1, le=90)):
    today = date.today().isoformat()
    warning = (date.today() + timedelta(days=days)).isoformat()
    result = sb.table("documents") \
        .select("*, crew!inner(company_id, full_name_ar, full_name_en)") \
        .eq("crew.company_id", current_user["company_id"]) \
        .lte("expiry_date", warning).gte("expiry_date", today) \
        .order("expiry_date").execute()
    return result.data or []


# ── Reminders (manual + cron) ─────────────────────────────────────────────────
def _scan_company_reminders(sb, company_id: str) -> dict:
    """Notify owners (+ compliance/ops managers) of every document in `company_id`
    expiring within the window. Idempotent per doc via `last_reminder_sent`."""
    now = datetime.now(timezone.utc)
    today = now.date()
    cutoff = (today + timedelta(days=_REMINDER_WINDOW_DAYS)).isoformat()
    cooldown = (now - timedelta(days=_REMINDER_COOLDOWN_DAYS)).isoformat()

    docs = (sb.table("documents")
            .select("*, crew!inner(id, company_id, full_name_ar, full_name_en)")
            .eq("crew.company_id", company_id)
            .lte("expiry_date", cutoff).gte("expiry_date", today.isoformat())
            .execute().data) or []
    if not docs:
        return {"sent": 0, "skipped": 0, "checked": 0}

    crew_ids = list({d["crew_id"] for d in docs})
    users = (sb.table("users").select("id, crew_id, role")
             .eq("company_id", company_id).eq("is_active", True).execute().data) or []
    crew_to_user = {u["crew_id"]: u["id"] for u in users if u.get("crew_id")}
    # Compliance / management get a copy so renewals actually get actioned.
    managers = [u["id"] for u in users
                if u.get("role") in {"admin", "ops_manager", "compliance_officer"} and u.get("id")]

    sent = skipped = 0
    notifs, updates, pushes = [], [], []
    for doc in docs:
        last = doc.get("last_reminder_sent")
        if last and last > cooldown:
            skipped += 1
            continue
        try:
            expiry = date.fromisoformat(str(doc["expiry_date"])[:10])
        except (ValueError, TypeError):
            skipped += 1
            continue

        days_left = (expiry - today).days
        crew = doc.get("crew") or {}
        crew_name = crew.get("full_name_ar") or crew.get("full_name_en") or ""
        label = _DOC_LABELS_AR.get(doc.get("document_type", ""), doc.get("document_type", "وثيقة"))
        title_ar = "تنبيه: وثيقة ستنتهي قريباً"
        if days_left <= 0:
            owner_msg = f"وثيقتك ({label}) منتهية الصلاحية"
            mgr_msg = f"{label} لـ {crew_name} منتهية الصلاحية وتمنع الجدولة"
        elif days_left == 1:
            owner_msg = f"وثيقتك ({label}) ستنتهي غداً"
            mgr_msg = f"{label} لـ {crew_name} ستنتهي غداً"
        else:
            owner_msg = f"وثيقتك ({label}) ستنتهي خلال {days_left} يوماً"
            mgr_msg = f"{label} لـ {crew_name} ستنتهي خلال {days_left} يوماً"

        targets: list[tuple[str, str]] = []
        owner_uid = crew_to_user.get(doc["crew_id"])
        if owner_uid:
            targets.append((owner_uid, owner_msg))
        for mid in managers:
            if mid != owner_uid:
                targets.append((mid, mgr_msg))
        if not targets:
            skipped += 1
            continue

        for uid, msg in targets:
            notifs.append({
                "id": str(uuid.uuid4()), "user_id": uid, "type": "document_expiring",
                "title_ar": title_ar, "title_en": "Document Expiring Soon",
                "message_ar": msg, "message_en": f"Document expiring in {days_left} days",
                "body_ar": msg, "body_en": f"Document expiring in {days_left} days",
                "reference_id": doc["id"], "reference_type": "document",
                "is_read": False, "created_at": now.isoformat(),
            })
            pushes.append((uid, title_ar, msg))
        updates.append(doc["id"])
        sent += 1

    if notifs:
        sb.table("notifications").insert(notifs).execute()
        for doc_id in updates:
            sb.table("documents").update({"last_reminder_sent": now.isoformat()}) \
                .eq("id", doc_id).execute()
        for uid, title, body in pushes:
            try:
                push_service.send_to_users(sb, [uid], title=title, body=body,
                                           data={"type": "document_expiring"})
            except Exception as e:
                log.warning("doc-expiry push failed for %s: %s", uid, e)

    return {"sent": sent, "skipped": skipped, "checked": len(docs)}


@router.post("/check-expiring-reminders", status_code=200)
async def check_expiring_reminders(current_user: CurrentUser, sb: SbClient):
    """Manual trigger (admin / ops manager) — reminders for THIS company."""
    if current_user["role"] not in {"admin", "ops_manager"} and not current_user.get("is_superuser"):
        raise ForbiddenError("الإدمن ومدير العمليات فقط")
    return _scan_company_reminders(sb, current_user["company_id"])


@router.get("/cron/expiry-reminders", status_code=200)
async def cron_expiry_reminders(sb: SbClient, authorization: str | None = Header(default=None)):
    """Scheduled trigger (Vercel Cron → GET). Authenticated by the CRON_SECRET
    bearer token (Vercel auto-adds `Authorization: Bearer $CRON_SECRET`), NOT a
    user. Scans EVERY active company so reminders don't depend on anyone opening
    a page."""
    secret = getattr(settings, "CRON_SECRET", "") or ""
    if not secret or authorization != f"Bearer {secret}":
        raise ForbiddenError("Invalid cron credentials")
    companies = (sb.table("companies").select("id").eq("is_active", True).execute().data) or []
    total = {"sent": 0, "skipped": 0, "checked": 0, "companies": 0}
    for c in companies:
        try:
            r = _scan_company_reminders(sb, c["id"])
            total["sent"] += r["sent"]
            total["skipped"] += r["skipped"]
            total["checked"] += r["checked"]
            total["companies"] += 1
        except Exception as e:
            log.warning("cron reminder failed for company %s: %s", c.get("id"), e)
    return total
