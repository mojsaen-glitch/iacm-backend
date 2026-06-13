"""Standby report endpoint — READ-ONLY (R6.1).

GET /reports/standby — aggregates existing `standby_assignments` rows into
per-crew monthly counts. NO writes, NO engine, NO flight-hours/FTL/FDP/payroll.
Company-scoped (super_admin/admin may target another company). Month follows the
BAGHDAD calendar, like the flight-hours reports. Empty month → empty report,
never an error.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError
from app.core.monthly_hours import _month_bounds_baghdad, _BAGHDAD
from app.core.standby_report import compute_standby_report
from app.core.standby_roster import generate_standby_roster_draft

router = APIRouter(prefix="/reports/standby", tags=["Standby Report"])
log = logging.getLogger(__name__)

# Same viewer population as the other operational reports.
_VIEW_ROLES = {"super_admin", "admin", "ops_manager", "scheduler",
               "scheduler_admin", "compliance_officer",
               "flight_operations", "flight_operations_admin", "flight_ops",
               "flight_movement", "flight_movement_admin"}


def _company_for(current_user: dict, company_id: Optional[str]) -> str:
    if company_id and current_user.get("role") in ("super_admin", "admin"):
        return company_id
    return current_user["company_id"]


def _build_report(sb, cid, y, m, base, rank, standby_type, status, now):
    """Shared report builder — the SINGLE data path used by both the JSON
    endpoint and the export, so their numbers are identical by construction."""
    start, end = _month_bounds_baghdad(y, m)
    q = (sb.table("standby_assignments").select("*")
         .eq("company_id", cid)
         .gte("start_time", start).lt("start_time", end))
    if standby_type:
        q = q.eq("standby_type", standby_type.upper())
    if status:
        q = q.eq("status", status.upper())
    rows = []
    try:
        rows = q.execute().data or []
    except Exception as e:
        log.warning("standby report query failed for %s: %s", cid, e)

    crew_ids = list({r.get("crew_id") for r in rows if r.get("crew_id")})
    crew_by_id: dict = {}
    if crew_ids:
        try:
            cres = (sb.table("crew")
                    .select("id,full_name_ar,full_name_en,rank,base")
                    .in_("id", crew_ids).execute().data) or []
            crew_by_id = {c["id"]: c for c in cres}
        except Exception as e:
            log.warning("standby report crew lookup failed for %s: %s", cid, e)
    if base:
        rows = [r for r in rows
                if (crew_by_id.get(r.get("crew_id"), {}).get("base") or "") == base]
    if rank:
        rows = [r for r in rows
                if (crew_by_id.get(r.get("crew_id"), {}).get("rank") or "") == rank]

    report = compute_standby_report(rows, crew_by_id, now)
    report.update({
        "company_id": cid, "year": y, "month": m,
        "filters": {"base": base, "rank": rank,
                    "standby_type": standby_type, "status": status},
    })
    return report


def _build_roster_draft(sb, cid, y, m, requirements, now):
    """Shared roster-draft builder (R6.3) — used by the JSON endpoint and the
    export. PREVIEW only: persists nothing."""
    crew_pool = (sb.table("crew")
                 .select("id,full_name_ar,full_name_en,rank,base")
                 .eq("company_id", cid).execute().data) or []
    crew_pool = [{"id": c["id"], "base": c.get("base"), "rank": c.get("rank"),
                  "name_ar": c.get("full_name_ar", ""),
                  "name_en": c.get("full_name_en", "")} for c in crew_pool]

    start, end = _month_bounds_baghdad(y, m)
    existing = []
    try:
        existing = (sb.table("standby_assignments").select("*")
                    .eq("company_id", cid)
                    .gte("start_time", start).lt("start_time", end)
                    .execute().data) or []
    except Exception as e:
        log.warning("roster-draft load query failed for %s: %s", cid, e)
    load_rep = compute_standby_report(existing, {}, now)
    base_load = {c["crew_id"]: c["shifts"] for c in load_rep["crew"]}

    from app.api.v1.endpoints.standby import _standby_eligibility
    _cache: dict = {}

    def is_eligible(crew_id, s_iso, e_iso):
        key = (crew_id, s_iso, e_iso)
        if key not in _cache:
            _cache[key] = _standby_eligibility(sb, crew_id, s_iso, e_iso)
        return _cache[key]

    draft = generate_standby_roster_draft(
        year=y, month=m, requirements=requirements,
        crew_pool=crew_pool, base_load=base_load, is_eligible=is_eligible)
    draft["company_id"] = cid
    return draft


@router.get("")
async def standby_report(current_user: CurrentUser, sb: SbClient,
                         year: Optional[int] = Query(None),
                         month: Optional[int] = Query(None, ge=1, le=12),
                         base: Optional[str] = Query(None),
                         rank: Optional[str] = Query(None),
                         standby_type: Optional[str] = Query(None),
                         status: Optional[str] = Query(None),
                         company_id: Optional[str] = Query(None)):
    if current_user.get("role") not in _VIEW_ROLES \
            and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرّح بعرض تقرير الاحتياط")
    cid = _company_for(current_user, company_id)
    now = datetime.now(timezone.utc)
    bag_now = now + _BAGHDAD
    return _build_report(sb, cid, year or bag_now.year, month or bag_now.month,
                         base, rank, standby_type, status, now)


# Same population that manages standby may PREVIEW a roster draft.
_PLAN_ROLES = {"super_admin", "admin", "ops_manager", "scheduler",
               "scheduler_admin", "crew_allocator", "cabin_allocator",
               "cockpit_allocator", "ground_allocator",
               "sched_captain", "sched_copilot", "sched_engineer",
               "sched_purser", "sched_cabin", "sched_balance",
               "sched_security", "sched_extra",
               "flight_movement", "flight_movement_admin"}


@router.post("/roster-draft")
async def standby_roster_draft(data: dict, current_user: CurrentUser, sb: SbClient):
    """R6.3 — generate a PROPOSED monthly standby roster and return it.
    PREVIEW ONLY: persists nothing, creates no standby/assignment/callout, never
    activates. Eligibility reuses R4 (`_standby_eligibility`); fairness reuses
    the R6.2 per-crew load. Uncovered slots come back with reasons.

    Body: {year, month, requirements: [{base, rank, standby_type?, per_day?,
    start_hour?, end_hour?}], company_id?}.
    """
    if current_user.get("role") not in _PLAN_ROLES \
            and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرّح بتوليد مسودة جدول الاحتياط")
    cid = _company_for(current_user, data.get("company_id"))

    now = datetime.now(timezone.utc)
    bag_now = now + _BAGHDAD
    y = int(data.get("year") or bag_now.year)
    m = int(data.get("month") or bag_now.month)
    if not (1 <= m <= 12):
        raise HTTPException(status_code=422, detail="month يجب أن يكون 1..12")
    requirements = data.get("requirements") or []
    if not isinstance(requirements, list) or not requirements:
        raise HTTPException(status_code=422,
                            detail="requirements مطلوبة (قائمة قاعدة/رتبة/عدد)")
    return _build_roster_draft(sb, cid, y, m, requirements, now)


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/export")
async def standby_export(data: dict, current_user: CurrentUser, sb: SbClient):
    """R6.4 — export the standby report (+ fairness, + optional roster-draft
    preview) as an .xlsx workbook. READ-ONLY: builds from the SAME R6.1/R6.2/R6.3
    data path and persists NOTHING. Only `xlsx` is supported for now (PDF is
    deferred). If `requirements` are supplied, the Roster Draft + Uncovered
    sheets are filled from a fresh preview; otherwise those sheets are headers
    only. Empty month → a clean workbook with an empty Summary, never an error.

    Body: {year?, month?, format?='xlsx', base?, rank?, standby_type?, status?,
    requirements?, company_id?}.
    """
    if current_user.get("role") not in _VIEW_ROLES \
            and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرّح بتصدير تقرير الاحتياط")
    fmt = str(data.get("format") or "xlsx").lower()
    if fmt != "xlsx":
        raise HTTPException(status_code=422,
                            detail="الصيغة المدعومة حالياً: xlsx فقط (PDF مؤجَّل)")
    cid = _company_for(current_user, data.get("company_id"))
    now = datetime.now(timezone.utc)
    bag_now = now + _BAGHDAD
    y = int(data.get("year") or bag_now.year)
    m = int(data.get("month") or bag_now.month)
    if not (1 <= m <= 12):
        raise HTTPException(status_code=422, detail="month يجب أن يكون 1..12")

    report = _build_report(sb, cid, y, m, data.get("base"), data.get("rank"),
                           data.get("standby_type"), data.get("status"), now)

    roster = None
    requirements = data.get("requirements") or []
    if isinstance(requirements, list) and requirements:
        roster = _build_roster_draft(sb, cid, y, m, requirements, now)

    from app.core.standby_export import build_standby_workbook
    content = build_standby_workbook(report, roster)
    filename = f"standby_{cid}_{y:04d}-{m:02d}.xlsx"
    return Response(
        content=content, media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
