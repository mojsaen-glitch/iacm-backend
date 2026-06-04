"""Operations Manual articles — admin-editable CRUD.

Read access is per-company and the visible_to_roles filter is applied
server-side so the client never receives clauses it shouldn't see.
Write access is admin / ops_manager only.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.om_governance import (
    gate_decision, governance_notification, is_safety_critical,
)

router = APIRouter(prefix="/om-articles", tags=["OM"])
log = logging.getLogger(__name__)

# Roles that receive a notification when a safety-critical clause is changed.
_MANAGEMENT_ROLES = ["super_admin", "admin", "ops_manager", "compliance_admin"]

_EDITOR_ROLES = {"super_admin", "admin", "ops_manager"}
_ALLOWED_FIELDS = {
    "section", "chapter_ar", "chapter_en", "title_ar", "title_en",
    "body_ar", "body_en", "visible_to_roles", "linked_rule_id",
    "linked_route", "sort_order", "is_active",
    # OM-as-rules-engine governance fields
    "rule_type", "category", "affects_compliance", "bound_check_key",
    # Structured operational values the engine applies (text stays documentation)
    "parameters",
}
_RULE_TYPES = {"informational", "warning", "blocking", "approval_required"}

# Registry of compliance-engine check families an OM clause can BIND to (via
# bound_check_key). Mirrors ComplianceEngine._binding_key — keep in sync.
CHECK_KEYS = [
    {"key": "crew_status",          "label_ar": "حالة الطاقم (نشط/إجازة/موقوف)", "label_en": "Crew status"},
    {"key": "documents",            "label_ar": "صلاحية الوثائق",               "label_en": "Document validity"},
    {"key": "training",             "label_ar": "صلاحية التدريب",               "label_en": "Training validity"},
    {"key": "flight_hours_28day",   "label_ar": "حد 28 يوم للساعات",            "label_en": "28-day hours cap"},
    {"key": "flight_hours_yearly",  "label_ar": "الحد السنوي للساعات",          "label_en": "Annual hours cap"},
    {"key": "flight_hours_monthly", "label_ar": "الحد الشهري للساعات",          "label_en": "Monthly hours cap"},
    {"key": "rest",                 "label_ar": "الراحة بين الواجبات",           "label_en": "Rest between duties"},
    {"key": "fdp",                  "label_ar": "فترة العمل الجوي FDP",          "label_en": "Flight Duty Period"},
    {"key": "aircraft_qualification","label_ar": "التأهيل على نوع الطائرة",      "label_en": "Aircraft type rating"},
    {"key": "assignment_conflict",  "label_ar": "تعارض التكليفات الزمني",        "label_en": "Assignment time conflict"},
]


def _ensure_editor(user: dict) -> None:
    if user.get("role") not in _EDITOR_ROLES:
        raise ForbiddenError("Only admin / ops_manager can edit the Operations Manual")


def _validate_rule_type(data: dict) -> None:
    rt = data.get("rule_type")
    if rt is not None and rt not in _RULE_TYPES:
        raise HTTPException(status_code=422,
            detail=f"rule_type must be one of {sorted(_RULE_TYPES)}")


# Parameter keys that must be strictly POSITIVE numbers.
_POSITIVE_PARAMS = (
    "max_hours", "domestic_min_rest_hours", "international_min_rest_hours",
    "validity_months", "warning_before_days", "rolling_window_days",
    "max_fdp_minutes", "yearly_window_days", "sectors_count",
)


def _validate_parameters(data: dict) -> None:
    """Reject illogical operational values before they reach the engine."""
    params = data.get("parameters")
    if params is None:
        return
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="parameters must be an object")

    def _isnum(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for k in _POSITIVE_PARAMS:
        if k in params and (not _isnum(params[k]) or params[k] <= 0):
            raise HTTPException(status_code=422, detail=f"{k} يجب أن تكون قيمة موجبة")
    if "max_turnaround_hours" in params:
        v = params["max_turnaround_hours"]
        if not _isnum(v) or v < 0:
            raise HTTPException(status_code=422, detail="max_turnaround_hours لا يمكن أن تكون سالبة")
    if "warning_threshold_percent" in params:
        v = params["warning_threshold_percent"]
        if not _isnum(v) or not (1 <= v <= 100):
            raise HTTPException(status_code=422, detail="warning_threshold_percent يجب أن تكون بين 1 و100")


def _audit(sb, *, article_id: str, company_id, action: str, user_id,
           before: dict | None, after: dict | None, note: str | None = None,
           governance_reason: str | None = None,
           safety_critical: bool = False) -> None:
    """Best-effort governance audit. Never fails the caller's request."""
    row = {
        "id":                 str(uuid.uuid4()),
        "article_id":         article_id,
        "company_id":         company_id,
        "action":             action,
        "changed_by":         user_id,
        "before":             before,
        "after":              after,
        "note":               note,
        "governance_reason":  governance_reason,
        "is_safety_critical": safety_critical,
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }
    try:
        sb.table("om_rule_audit_logs").insert(row).execute()
    except Exception:
        # The governance columns may not be migrated yet — retry without them so
        # the core audit trail is never lost.
        try:
            row.pop("governance_reason", None)
            row.pop("is_safety_critical", None)
            sb.table("om_rule_audit_logs").insert(row).execute()
        except Exception:
            log.exception("OM audit log write failed for %s", article_id)


def _notify_management(sb, *, company_id, article_id: str, changed_by_name: str,
                       kind: str | None, reason: str) -> None:
    """Best-effort: notify management that a safety-critical clause changed."""
    try:
        users = sb.table("users").select("id") \
            .eq("company_id", company_id) \
            .in_("role", _MANAGEMENT_ROLES).execute().data or []
        if not users:
            return
        n = governance_notification(article_id, changed_by_name, kind, reason)
        now = datetime.now(timezone.utc).isoformat()
        rows = [{
            "id":             str(uuid.uuid4()),
            "user_id":        u["id"],
            "target_user_id": u["id"],
            "company_id":     company_id,
            "type":           "om_governance_change",
            "title_ar":       n["title_ar"],
            "title_en":       n["title_en"],
            "message_ar":     n["message_ar"],
            "message_en":     n["message_en"],
            "body_ar":        n["message_ar"],
            "body_en":        n["message_en"],
            "reference_id":   article_id,
            "reference_type": "om_article",
            "is_read":        False,
            "created_at":     now,
            "updated_at":     now,
        } for u in users]
        sb.table("notifications").insert(rows).execute()
    except Exception:
        log.exception("OM governance notification failed for %s", article_id)


@router.get("")
async def list_articles(current_user: CurrentUser, sb: SbClient):
    """Return all OM articles visible to the caller's role + company.

    Filtering is intentionally done in Python rather than at SQL — the
    `visible_to_roles` array stores an empty list to mean "everyone",
    and PostgREST array filters can't express that cleanly. The table
    is small (a few dozen rows), so this is fine.
    """
    company_id = current_user["company_id"]
    res = sb.table("om_articles") \
        .select("*") \
        .eq("company_id", company_id) \
        .eq("is_active", True) \
        .order("section") \
        .order("sort_order") \
        .order("id") \
        .execute()

    role = current_user.get("role", "")
    out = []
    for row in (res.data or []):
        roles = row.get("visible_to_roles") or []
        if not roles or role in roles:
            out.append(row)
    return out


# ── Global FTL presets (ADVISORY — must be reviewed/approved before use) ──────
# Reference frameworks differ in the fine print (FAA Part 117, EASA Subpart-FTL,
# ICAO Annex 6, IATA FRMS). These are starting templates, NOT final values.
OM_PRESETS = [
    {"key": "icao_28d_hours", "reference": "ICAO", "rule_kind": "flight_hours_limit",
     "bound_check_key": "flight_hours_28day", "rule_type": "blocking", "category": "fatigue",
     "label_ar": "ICAO — حد 28 يوم للساعات", "label_en": "ICAO Flight Hours 28 Days",
     "parameters": {"reference": "ICAO", "rule_kind": "flight_hours_limit",
                    "rolling_window_days": 28, "max_hours": 100, "warning_threshold_percent": 90}},
    {"key": "icao_annual_hours", "reference": "ICAO", "rule_kind": "flight_hours_limit",
     "bound_check_key": "flight_hours_yearly", "rule_type": "blocking", "category": "fatigue",
     "label_ar": "ICAO — الحد السنوي للساعات", "label_en": "ICAO Annual Flight Hours",
     "parameters": {"reference": "ICAO", "rule_kind": "flight_hours_limit",
                    "rolling_window_days": 365, "max_hours": 900, "warning_threshold_percent": 89}},
    {"key": "min_rest_domestic", "reference": "ICAO", "rule_kind": "minimum_rest",
     "bound_check_key": "rest", "rule_type": "blocking", "category": "fatigue",
     "label_ar": "الراحة الدنيا — محلي", "label_en": "Minimum Rest Domestic",
     "parameters": {"reference": "ICAO", "rule_kind": "minimum_rest",
                    "domestic_min_rest_hours": 10, "rest_based_on_previous_duty": True}},
    {"key": "min_rest_intl", "reference": "ICAO", "rule_kind": "minimum_rest",
     "bound_check_key": "rest", "rule_type": "blocking", "category": "fatigue",
     "label_ar": "الراحة الدنيا — دولي", "label_en": "Minimum Rest International",
     "parameters": {"reference": "ICAO", "rule_kind": "minimum_rest",
                    "international_min_rest_hours": 12, "rest_based_on_previous_duty": True}},
    {"key": "fdp_table", "reference": "EASA", "rule_kind": "fdp_limit",
     "bound_check_key": "fdp", "rule_type": "blocking", "category": "fatigue",
     "label_ar": "FDP حسب وقت البدء والقطاعات", "label_en": "FDP By Start Time And Sectors",
     "parameters": {"reference": "EASA", "rule_kind": "fdp_limit",
                    "source": "fdp_rules_table"}},
    {"key": "training_validity", "reference": "ICAO", "rule_kind": "training_validity",
     "bound_check_key": "training", "rule_type": "blocking", "category": "training",
     "label_ar": "صلاحية التدريب", "label_en": "Training Validity",
     "parameters": {"reference": "ICAO", "rule_kind": "training_validity",
                    "validity_months": 12, "block_if_expired": True}},
    {"key": "aircraft_qual", "reference": "ICAO", "rule_kind": "aircraft_qualification",
     "bound_check_key": "aircraft_qualification", "rule_type": "blocking", "category": "aircraft",
     "label_ar": "التأهيل على نوع الطائرة", "label_en": "Aircraft Qualification",
     "parameters": {"reference": "ICAO", "rule_kind": "aircraft_qualification",
                    "block_if_unrated": True}},
]


@router.get("/check-keys")
async def list_check_keys(current_user: CurrentUser):
    """The compliance-engine checks an OM clause can bind to (UI dropdown)."""
    _ensure_editor(current_user)
    return CHECK_KEYS


@router.get("/presets")
async def list_presets(current_user: CurrentUser):
    """Advisory global FTL templates (ICAO/EASA/FAA). NOT final — a compliance
    officer must review/approve the values before they go live."""
    _ensure_editor(current_user)
    return {"advisory": True,
            "note_ar": "يجب مراجعة واعتماد القيم من مسؤول الامتثال قبل التشغيل الرسمي",
            "note_en": "Values must be reviewed and approved by Compliance before going live",
            "presets": OM_PRESETS}


@router.get("/admin/all")
async def list_all_articles(current_user: CurrentUser, sb: SbClient):
    """Control-center listing: ALL clauses incl. inactive, with governance
    fields. Editor-only (the public GET hides inactive + filters by role)."""
    _ensure_editor(current_user)
    res = sb.table("om_articles") \
        .select("*") \
        .eq("company_id", current_user["company_id"]) \
        .order("section").order("sort_order").order("id") \
        .execute()
    return res.data or []


@router.get("/{article_id:path}/audit")
async def article_audit(article_id: str, current_user: CurrentUser, sb: SbClient):
    """Change history for one clause (most recent first)."""
    _ensure_editor(current_user)
    res = sb.table("om_rule_audit_logs") \
        .select("*") \
        .eq("article_id", article_id) \
        .order("created_at", desc=True) \
        .execute()
    return res.data or []


@router.post("", status_code=201)
async def create_article(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)
    _validate_rule_type(data)
    _validate_parameters(data)

    art_id = (data.get("id") or "").strip()
    if not art_id:
        raise HTTPException(status_code=422, detail="id is required (e.g. 'OM-A 9.1')")

    section = (data.get("section") or "").strip().upper()
    if section not in {"A", "B", "C", "D"}:
        raise HTTPException(status_code=422, detail="section must be one of A/B/C/D")

    # Duplicate check
    existing = sb.table("om_articles").select("id").eq("id", art_id).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail=f"Article '{art_id}' already exists")

    now = datetime.now(timezone.utc).isoformat()
    row = {k: v for k, v in data.items() if k in _ALLOWED_FIELDS}
    row.update({
        "id":            art_id,
        "section":       section,
        "company_id":    current_user["company_id"],
        "title_ar":      data.get("title_ar", ""),
        "title_en":      data.get("title_en", ""),
        "body_ar":       data.get("body_ar", ""),
        "body_en":       data.get("body_en", ""),
        "created_by":    current_user["id"],
        "updated_by":    current_user["id"],
        "created_at":    now,
        "updated_at":    now,
    })

    if not row["title_ar"] and not row["title_en"]:
        raise HTTPException(status_code=422, detail="At least one title (ar/en) is required")

    # Safety-governance gate — creating a critical binding with a non-blocking
    # type would sneak in a weak rule that overrides the family.
    governance_reason = (data.get("governance_reason") or "").strip()
    status, kind = gate_decision(current_user, {}, row, governance_reason)
    safety_critical = is_safety_critical(row.get("affects_compliance"),
                                         row.get("bound_check_key"))
    if status == "forbidden":
        raise ForbiddenError(
            "ربط فحص أمان حرج بقاعدة غير حاجبة يتطلب صلاحية مشرف عام (Super Admin)")
    if status == "reason_required":
        raise HTTPException(status_code=422,
            detail="سبب التغيير (governance_reason) مطلوب لبند أمان حرج")

    try:
        res = sb.table("om_articles").insert(row).execute()
    except Exception as e:
        log.exception("create OM article failed")
        raise HTTPException(status_code=502, detail=f"insert failed: {str(e)[:200]}")
    saved = res.data[0] if res.data else row
    _audit(sb, article_id=art_id, company_id=current_user["company_id"],
           action="create", user_id=current_user["id"], before=None, after=saved,
           governance_reason=governance_reason or None, safety_critical=safety_critical)
    if safety_critical:
        _notify_management(sb, company_id=current_user["company_id"], article_id=art_id,
                           changed_by_name=current_user.get("full_name")
                               or current_user.get("email") or current_user["id"],
                           kind=kind or "create", reason=governance_reason or "—")
    return saved


@router.patch("/{article_id:path}")
async def update_article(
    article_id: str, data: dict, current_user: CurrentUser, sb: SbClient
):
    _ensure_editor(current_user)
    _validate_rule_type(data)
    _validate_parameters(data)

    company_id = current_user["company_id"]
    existing = sb.table("om_articles") \
        .select("*") \
        .eq("id", article_id) \
        .eq("company_id", company_id) \
        .execute()
    if not existing.data:
        raise NotFoundError("OM article", article_id)
    before = existing.data[0]

    update = {k: v for k, v in data.items() if k in _ALLOWED_FIELDS}
    update["updated_by"] = current_user["id"]

    if "section" in update:
        update["section"] = (update["section"] or "").strip().upper()
        if update["section"] not in {"A", "B", "C", "D"}:
            raise HTTPException(status_code=422, detail="section must be one of A/B/C/D")

    # ── Safety-governance gate ───────────────────────────────────────
    # Compute the resulting state, then block any WEAKENING of a
    # safety-critical clause unless the caller is Super Admin and gives a reason.
    after_state = {**before, **update}
    governance_reason = (data.get("governance_reason") or "").strip()
    status, kind = gate_decision(current_user, before, after_state, governance_reason)
    safety_critical = (
        is_safety_critical(before.get("affects_compliance"), before.get("bound_check_key"))
        or is_safety_critical(after_state.get("affects_compliance"),
                              after_state.get("bound_check_key"))
    )
    if status == "forbidden":
        raise ForbiddenError(
            "تغيير بند أمان حرج (تعطيل/تخفيض/إعادة ربط) يتطلب صلاحية مشرف عام (Super Admin)")
    if status == "reason_required":
        raise HTTPException(status_code=422,
            detail="سبب التغيير (governance_reason) مطلوب عند تعديل بند أمان حرج")

    try:
        res = sb.table("om_articles") \
            .update(update) \
            .eq("id", article_id) \
            .eq("company_id", company_id) \
            .execute()
    except Exception as e:
        log.exception("update OM article failed")
        raise HTTPException(status_code=502, detail=f"update failed: {str(e)[:200]}")
    after = res.data[0] if res.data else update
    # "toggle" when only the active flag changed — clearer in the audit trail.
    action = "toggle" if set(update.keys()) <= {"is_active", "updated_by"} else "update"
    _audit(sb, article_id=article_id, company_id=company_id, action=action,
           user_id=current_user["id"], before=before, after=after,
           governance_reason=governance_reason or None,
           safety_critical=safety_critical)
    # Notify management whenever a safety-critical clause is touched.
    if safety_critical:
        _notify_management(sb, company_id=company_id, article_id=article_id,
                           changed_by_name=current_user.get("full_name")
                               or current_user.get("email") or current_user["id"],
                           kind=kind, reason=governance_reason or "—")
    return after


@router.delete("/{article_id:path}", status_code=204)
async def delete_article(article_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)

    company_id = current_user["company_id"]
    res = sb.table("om_articles") \
        .delete() \
        .eq("id", article_id) \
        .eq("company_id", company_id) \
        .execute()
    if not res.data:
        raise NotFoundError("OM article", article_id)
    _audit(sb, article_id=article_id, company_id=company_id, action="delete",
           user_id=current_user["id"], before=res.data[0], after=None)
    return None


@router.post("/seed", status_code=201)
async def seed_defaults(data: dict, current_user: CurrentUser, sb: SbClient):
    """Bulk-import the static catalog from the client.

    The client sends `articles: [...]`; we upsert by id so re-seeding is
    safe and only fills in missing rows. Used the first time an admin
    opens the Settings page to populate the table from the ship-with-binary
    defaults.
    """
    _ensure_editor(current_user)

    articles = data.get("articles") or []
    if not isinstance(articles, list) or not articles:
        raise HTTPException(status_code=422, detail="articles[] is required")

    company_id = current_user["company_id"]
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for a in articles:
        rows.append({
            "id":               a.get("id"),
            "company_id":       company_id,
            "section":          (a.get("section") or "A").strip().upper(),
            "chapter_ar":       a.get("chapter_ar", ""),
            "chapter_en":       a.get("chapter_en", ""),
            "title_ar":         a.get("title_ar", ""),
            "title_en":         a.get("title_en", ""),
            "body_ar":          a.get("body_ar", ""),
            "body_en":          a.get("body_en", ""),
            "visible_to_roles": a.get("visible_to_roles") or [],
            "linked_rule_id":   a.get("linked_rule_id"),
            "linked_route":     a.get("linked_route"),
            "rule_type":        a.get("rule_type", "informational"),
            "category":         a.get("category"),
            "affects_compliance": bool(a.get("affects_compliance", False)),
            "bound_check_key":  a.get("bound_check_key"),
            "sort_order":       a.get("sort_order", 0),
            "is_active":        True,
            "created_by":       current_user["id"],
            "updated_by":       current_user["id"],
            "created_at":       now,
            "updated_at":       now,
        })

    inserted = 0
    skipped  = 0
    for r in rows:
        try:
            existing = sb.table("om_articles").select("id").eq("id", r["id"]).execute()
            if existing.data:
                skipped += 1
                continue
            sb.table("om_articles").insert(r).execute()
            inserted += 1
        except Exception:
            log.exception("seed row failed for %s", r.get("id"))
    return {"inserted": inserted, "skipped": skipped, "total": len(rows)}
