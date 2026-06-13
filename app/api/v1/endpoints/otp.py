"""On-Time Performance report endpoint — READ-ONLY.

GET /reports/otp — aggregates existing flight rows (no writes, no engine, no
GD/hours/FTL). Company-scoped; super_admin/admin may target another company.
Filters: from/to (departure window), aircraft_type, registration, reason code.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Query

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError
from app.core.otp_report import compute_otp, DEFAULT_OTP_THRESHOLD_MIN

router = APIRouter(prefix="/reports/otp", tags=["OTP"])
log = logging.getLogger(__name__)

_VIEW_ROLES = {"super_admin", "admin", "ops_manager", "scheduler",
               "scheduler_admin", "compliance_officer",
               "flight_operations", "flight_operations_admin", "flight_ops",
               "flight_movement", "flight_movement_admin"}


def _company_for(current_user: dict, company_id: Optional[str]) -> str:
    if company_id and current_user.get("role") in ("super_admin", "admin"):
        return company_id
    return current_user["company_id"]


@router.get("")
async def otp_report(current_user: CurrentUser, sb: SbClient,
                     date_from: Optional[str] = Query(None),
                     date_to: Optional[str] = Query(None),
                     aircraft_type: Optional[str] = Query(None),
                     registration: Optional[str] = Query(None),
                     reason_code: Optional[str] = Query(None),
                     threshold_min: int = Query(DEFAULT_OTP_THRESHOLD_MIN,
                                                ge=0, le=180),
                     company_id: Optional[str] = Query(None)):
    if current_user.get("role") not in _VIEW_ROLES \
            and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرّح بعرض تقرير دقة المواعيد")
    cid = _company_for(current_user, company_id)

    q = (sb.table("flights").select(
            "id, flight_number, origin_code, destination_code, aircraft_type, "
            "aircraft_registration, status, departure_time, arrival_time, "
            "estimated_departure_time, actual_departure_time, "
            "actual_arrival_time, delay_minutes, delay_reason_code")
         .eq("company_id", cid))
    if date_from:
        q = q.gte("departure_time", date_from)
    if date_to:
        q = q.lte("departure_time", f"{date_to}T23:59:59")
    if aircraft_type:
        q = q.eq("aircraft_type", aircraft_type)
    if registration:
        q = q.eq("aircraft_registration", registration)
    if reason_code:
        q = q.eq("delay_reason_code", reason_code)

    rows = []
    try:
        rows = q.order("departure_time").execute().data or []
    except Exception as e:
        log.warning("OTP query failed for %s: %s", cid, e)

    summary = compute_otp(rows, threshold_min=threshold_min)
    summary["company_id"] = cid
    summary["filters"] = {
        "date_from": date_from, "date_to": date_to,
        "aircraft_type": aircraft_type, "registration": registration,
        "reason_code": reason_code,
    }
    return summary
