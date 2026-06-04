"""Crew Monthly Flight Hours Matrix — read API + professional Excel export.

Phase 1: computed (read-only) monthly matrix from ``flights.duration_hours`` via
the ``credited_hours`` abstraction, plus a 5-sheet openpyxl export. Manual edit +
audit log + advanced dashboard + PDF come in a later phase.

RBAC: admin / super_admin / ops_manager / scheduler may VIEW; export is limited to
admin / super_admin / ops_manager.
"""
import uuid
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Query, Response

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError, ValidationError
from app.core.monthly_hours import build_matrix, build_statement, invalidate_matrix_cache
from app.core.monthly_hours_excel import build_workbook, build_statement_workbook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/reports/monthly-hours", tags=["Monthly Hours"])

_VIEW_ROLES = {"super_admin", "admin", "ops_manager", "scheduler", "scheduler_admin"}
_EXPORT_ROLES = {"super_admin", "admin", "ops_manager"}
_EDIT_ROLES = {"super_admin"}                 # manual hour edit
_AUDIT_VIEW_ROLES = {"super_admin", "admin"}  # who can read the audit log

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _ensure(role: str, allowed: set, what: str) -> None:
    if role not in allowed:
        raise ForbiddenError(f"غير مصرّح بـ {what} لإحصائية ساعات الطيران")


def _filters(crew_type, rank, aircraft_type, base, search,
             only_with_hours, show_grounded, violations_only, include_inactive,
             dh_credit=None) -> dict:
    return {
        "crew_type": crew_type, "rank": rank, "aircraft_type": aircraft_type,
        "base": base, "search": search,
        "only_with_hours": only_with_hours, "show_grounded": show_grounded,
        "violations_only": violations_only, "include_inactive": include_inactive,
        "dh_credit": dh_credit,
    }


def _company_for(current_user: dict, company_id: str | None) -> str:
    # Only super_admin/admin may target another company; everyone else is scoped.
    if company_id and current_user.get("role") in ("super_admin", "admin"):
        return company_id
    return current_user["company_id"]


def _company_name(sb, company_id: str) -> str:
    try:
        res = sb.table("companies").select("name").eq("id", company_id).limit(1).execute()
        if res.data:
            return res.data[0].get("name") or ""
    except Exception as e:
        log.info("company name lookup failed: %s", e)
    return ""


@router.get("/matrix")
async def monthly_matrix(
    current_user: CurrentUser, sb: SbClient,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    company_id: str | None = None,
    crew_type: str | None = None,        # pilots | cabin | all
    rank: str | None = None,
    aircraft_type: str | None = None,
    base: str | None = None,
    search: str | None = None,
    only_with_hours: bool = False,
    show_grounded: bool = True,
    violations_only: bool = False,
    include_inactive: bool = False,
    dh_credit: str | None = None,
):
    _ensure(current_user.get("role"), _VIEW_ROLES, "العرض")
    cid = _company_for(current_user, company_id)
    matrix = build_matrix(sb, cid, year, month, _filters(
        crew_type, rank, aircraft_type, base, search,
        only_with_hours, show_grounded, violations_only, include_inactive, dh_credit))
    matrix["company_id"] = cid
    return matrix


@router.get("/export")
async def export_matrix(
    current_user: CurrentUser, sb: SbClient,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    company_id: str | None = None,
    crew_type: str | None = None,
    rank: str | None = None,
    aircraft_type: str | None = None,
    base: str | None = None,
    search: str | None = None,
    only_with_hours: bool = False,
    show_grounded: bool = True,
    violations_only: bool = False,
    include_inactive: bool = False,
    dh_credit: str | None = None,
):
    _ensure(current_user.get("role"), _EXPORT_ROLES, "التصدير")
    cid = _company_for(current_user, company_id)
    matrix = build_matrix(sb, cid, year, month, _filters(
        crew_type, rank, aircraft_type, base, search,
        only_with_hours, show_grounded, violations_only, include_inactive, dh_credit))
    data = build_workbook(matrix, _company_name(sb, cid))
    filename = f"Crew_Hours_Matrix_{year:04d}_{month:02d}.xlsx"
    return Response(
        content=data,
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Crew Hours Legal Statement (per-crew traceable breakdown) ───────────────
@router.get("/statement")
async def crew_statement(
    current_user: CurrentUser, sb: SbClient,
    crew_id: str = Query(...),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    company_id: str | None = None,
    dh_credit: str | None = None,
):
    _ensure(current_user.get("role"), _VIEW_ROLES, "العرض")
    cid = _company_for(current_user, company_id)
    stmt = build_statement(sb, cid, crew_id, year, month, dh_credit)
    stmt["generated_by"] = _actor_name(current_user)
    stmt["generated_at"] = datetime.now(timezone.utc).isoformat()
    return stmt


@router.get("/statement/export")
async def crew_statement_export(
    current_user: CurrentUser, sb: SbClient,
    crew_id: str = Query(...),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    company_id: str | None = None,
    dh_credit: str | None = None,
):
    _ensure(current_user.get("role"), _VIEW_ROLES, "التصدير")
    cid = _company_for(current_user, company_id)
    stmt = build_statement(sb, cid, crew_id, year, month, dh_credit)
    gen_at = datetime.now(timezone.utc).isoformat()
    data = build_statement_workbook(stmt, _company_name(sb, cid), _actor_name(current_user), gen_at)
    code = (stmt["crew"].get("code") or crew_id) or "crew"
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(code))[:24]
    filename = f"Crew_Hours_Statement_{safe}_{year:04d}_{month:02d}.xlsx"
    return Response(
        content=data,
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Manual hour edit + audit log (super-admin) ──────────────────────────────
def _actor_name(user: dict) -> str:
    return (user.get("full_name") or user.get("name") or user.get("email")
            or user.get("id") or "")


def _audit(sb, company_id, crew_id, duty_date, action, old_value, new_value,
           reason, note, user):
    try:
        sb.table("crew_hours_audit_log").insert({
            "id": str(uuid.uuid4()),
            "company_id": company_id, "crew_id": crew_id, "duty_date": duty_date,
            "action": action,
            "old_value": float(old_value) if old_value is not None else None,
            "new_value": float(new_value) if new_value is not None else None,
            "reason": reason or "", "note": note or "",
            "performed_by": user.get("id"), "performed_by_name": _actor_name(user),
            "performed_role": user.get("role"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.info("crew hours audit skipped: %s", e)


def _duty_date(data: dict) -> str:
    y, m, d = int(data["year"]), int(data["month"]), int(data["day"])
    if not (1 <= m <= 12 and 1 <= d <= 31):
        raise ValidationError("تاريخ غير صالح")
    return f"{y:04d}-{m:02d}-{d:02d}"


@router.post("/override")
async def set_override(data: dict, current_user: CurrentUser, sb: SbClient):
    """Override the credited hours of a crew member on a day. Super-admin only.
    The reason is mandatory; every change is written to the audit log."""
    if current_user.get("role") not in _EDIT_ROLES:
        raise ForbiddenError("تعديل الساعات يدوياً متاح لـ Super Admin فقط")
    crew_id = (data.get("crew_id") or "").strip()
    reason = (data.get("reason") or "").strip()
    if not crew_id:
        raise ValidationError("crew_id مطلوب")
    if not reason:
        raise ValidationError("سبب التعديل إجباري")
    duty_date = _duty_date(data)
    hours = float(data.get("hours") or 0)
    if hours < 0:
        raise ValidationError("الساعات لا يمكن أن تكون سالبة")
    old_value = data.get("old_value")
    cid = current_user["company_id"]
    sb.table("crew_hours_overrides").upsert({
        "company_id": cid, "crew_id": crew_id, "duty_date": duty_date,
        "override_hours": hours,
        "old_value": float(old_value) if old_value is not None else None,
        "reason": reason, "note": data.get("note") or "",
        "created_by": current_user.get("id"), "created_by_name": _actor_name(current_user),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="company_id,crew_id,duty_date").execute()
    _audit(sb, cid, crew_id, duty_date, "set", old_value, hours, reason, data.get("note"), current_user)
    invalidate_matrix_cache(cid)   # overrides must show on the next matrix read
    return {"ok": True, "duty_date": duty_date, "override_hours": hours}


@router.post("/override/clear")
async def clear_override(data: dict, current_user: CurrentUser, sb: SbClient):
    """Remove a manual override → the day reverts to the computed hours."""
    if current_user.get("role") not in _EDIT_ROLES:
        raise ForbiddenError("تعديل الساعات يدوياً متاح لـ Super Admin فقط")
    crew_id = (data.get("crew_id") or "").strip()
    if not crew_id:
        raise ValidationError("crew_id مطلوب")
    duty_date = _duty_date(data)
    cid = current_user["company_id"]
    old = None
    try:
        ex = sb.table("crew_hours_overrides").select("override_hours") \
            .eq("company_id", cid).eq("crew_id", crew_id).eq("duty_date", duty_date).execute()
        if ex.data:
            old = ex.data[0].get("override_hours")
    except Exception as e:
        log.info("override lookup failed: %s", e)
    sb.table("crew_hours_overrides").delete() \
        .eq("company_id", cid).eq("crew_id", crew_id).eq("duty_date", duty_date).execute()
    _audit(sb, cid, crew_id, duty_date, "clear", old, None, data.get("reason"), data.get("note"), current_user)
    invalidate_matrix_cache(cid)
    return {"ok": True}


@router.get("/audit")
async def hours_audit(
    current_user: CurrentUser, sb: SbClient,
    crew_id: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
):
    """Audit trail of manual hour edits for a crew member (newest first)."""
    if current_user.get("role") not in _AUDIT_VIEW_ROLES:
        raise ForbiddenError("سجل التعديلات متاح للإدارة فقط")
    cid = current_user["company_id"]
    try:
        res = sb.table("crew_hours_audit_log").select("*") \
            .eq("company_id", cid).eq("crew_id", crew_id) \
            .order("created_at", desc=True).limit(limit).execute()
        return {"items": res.data or []}
    except Exception as e:
        log.info("audit read failed: %s", e)
        return {"items": []}
