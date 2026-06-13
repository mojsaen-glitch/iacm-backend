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

from fastapi import APIRouter, Query

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError
from app.core.monthly_hours import _month_bounds_baghdad, _BAGHDAD
from app.core.standby_report import compute_standby_report

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

    # Default to the current BAGHDAD calendar month.
    now = datetime.now(timezone.utc)
    bag_now = now + _BAGHDAD
    y = year or bag_now.year
    m = month or bag_now.month
    start, end = _month_bounds_baghdad(y, m)

    # Reserves whose window STARTS in the month (Baghdad), company-scoped.
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

    # Crew lookup for name/rank/base + the optional base/rank narrowing.
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
