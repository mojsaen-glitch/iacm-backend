import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ConflictError, FTLViolationError, CrewBlockedError, ForbiddenError
from app.core.config import settings
from app.core.compliance_engine import ComplianceEngine, IRAQI_AIRPORTS
from app.api.v1.endpoints.incompatibility import get_approved_dnp_pairs
from app.services import push_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/assignments", tags=["Crew Assignments"])

COCKPIT_RANKS = frozenset({"captain", "first_officer", "second_officer", "flight_engineer"})
CABIN_RANKS   = frozenset({"chief", "purser", "senior", "cabin_crew"})
GROUND_RANKS  = frozenset({"dispatcher", "ground_staff"})


@router.get("")
async def get_assignments(
    current_user: CurrentUser,
    sb: SbClient,
    flight_id: Optional[str] = Query(None),
    crew_id: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    page_size: int = Query(100),
):
    """Get assignments filtered by flight_id or crew_id with optional date range.

    The assignments table itself has no company_id column; we scope by company
    via a PostgREST inner-join on flights, which embeds the flight as
    `flights` on every row.
    """
    company_id = current_user["company_id"]

    q = sb.table("assignments") \
        .select("*, flights!inner(*)") \
        .eq("flights.company_id", company_id)
    if flight_id:
        q = q.eq("flight_id", flight_id)
    if crew_id:
        q = q.eq("crew_id", crew_id)
    result = q.limit(page_size).execute()
    rows = result.data or []
    if not rows:
        return []

    # Apply date filter against the embedded flight (departure_time)
    output = []
    for row in rows:
        flight = row.get("flights") or {}
        dep = flight.get("departure_time", "")
        if dep and (from_date or to_date):
            dep_date = dep[:10]
            if from_date and dep_date < from_date:
                continue
            if to_date and dep_date > to_date:
                continue
        output.append(row)

    return output


@router.post("", status_code=201)
async def assign_crew(data: dict, current_user: CurrentUser, sb: SbClient):
    flight_id = data["flight_id"]
    crew_id = data["crew_id"]
    is_override = data.get("is_override", False)

    # Validate flight
    flight_res = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    # Validate crew
    crew_res = sb.table("crew").select("*").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not crew_res.data:
        raise NotFoundError("Crew member", crew_id)
    crew = crew_res.data[0]

    # ── Department restriction check ─────────────────────────
    # cabin_allocator can only assign cabin crew, cockpit_allocator only pilots, etc.
    user_role = current_user.get("role", "")
    crew_dept  = current_user.get("crew_department", "")
    crew_rank  = crew.get("rank", "")

    if user_role == "cabin_allocator" and crew_rank not in CABIN_RANKS and not is_override:
        raise ForbiddenError("مخصص الضيافة يمكنه فقط تكليف طاقم المقصورة")
    if user_role == "cockpit_allocator" and crew_rank not in COCKPIT_RANKS and not is_override:
        raise ForbiddenError("مخصص القيادة يمكنه فقط تكليف الطيارين")
    if user_role == "ground_allocator" and crew_rank not in GROUND_RANKS and not is_override:
        raise ForbiddenError("مخصص الأرضي يمكنه فقط تكليف الطاقم الأرضي")

    # Check duplicate
    dup = sb.table("assignments").select("id").eq("flight_id", flight_id).eq("crew_id", crew_id).execute()
    if dup.data:
        raise ConflictError(f"Crew member already assigned to flight {flight['flight_number']}")

    # ── Do-Not-Pair (DNP) check ──────────────────────────────
    if not is_override:
        # Get all crew already assigned to this flight
        existing_assignments = sb.table("assignments").select("crew_id").eq("flight_id", flight_id).execute()
        assigned_crew_ids = [r["crew_id"] for r in (existing_assignments.data or []) if r.get("crew_id")]

        if assigned_crew_ids:
            dnp_pairs = get_approved_dnp_pairs(sb, current_user["company_id"])
            for (a, b) in dnp_pairs:
                # Check if crew_id is in a DNP pair with any already-assigned crew
                for existing_id in assigned_crew_ids:
                    if (crew_id == a and existing_id == b) or (crew_id == b and existing_id == a):
                        from app.core.exceptions import ForbiddenError
                        raise ForbiddenError(
                            f"لا يمكن تكليف هذا العضو — يوجد قرار عدم تطيير (DNP) مع عضو مكلّف بنفس الرحلة"
                        )

    # ── Full Compliance Check ─────────────────────────────────
    if not is_override:
        dep_str = flight.get("departure_time", "")
        arr_str = flight.get("arrival_time", "")
        dep_dt = datetime.fromisoformat(dep_str.replace("Z", "+00:00")) if dep_str else None
        arr_dt = datetime.fromisoformat(arr_str.replace("Z", "+00:00")) if arr_str else None
        is_intl = (
            flight.get("origin_code", "").upper()      not in IRAQI_AIRPORTS or
            flight.get("destination_code", "").upper() not in IRAQI_AIRPORTS
        )

        engine = ComplianceEngine(sb)
        compliance = engine.check_crew(
            crew_id=crew_id,
            flight_id=flight_id,
            flight_departure=dep_dt,
            flight_arrival=arr_dt,
            is_international=is_intl,
        )

        if compliance.get("status") == "BLOCKED":
            reasons = "; ".join(compliance.get("blocking_reasons", ["Compliance violation"]))
            raise CrewBlockedError(crew.get("full_name_en", crew_id), reasons)

    # NOTE: assignments table has no `company_id` column — isolation is
    # enforced via the flight relationship instead (see get_assignments).
    assignment = {
        "id": str(uuid.uuid4()),
        "flight_id": flight_id,
        "crew_id": crew_id,
        "assigned_by": current_user["id"],
        "assignment_type": data.get("assignment_type", "regular"),
        "is_override": is_override,
        "override_reason": data.get("override_reason") if is_override else None,
        "acknowledged": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        result = sb.table("assignments").insert(assignment).execute()
    except Exception as e:
        # Surface the actual DB error instead of a generic 500 so the
        # frontend can show something useful to the operator.
        logger.exception("assignments INSERT failed crew=%s flight=%s", crew_id, flight_id)
        raise HTTPException(
            status_code=502,
            detail=f"تعذّر حفظ التكليف: {str(e)[:200]}",
        )
    saved = result.data[0] if result.data else {}

    # ── Notify the crew member ───────────────────────────────
    # Full-detail Arabic + English message, times shown in Baghdad (UTC+3)
    # because that's how Iraqi Airways crews plan their day.
    try:
        crew_user = sb.table("users").select("id").eq("crew_id", crew_id).execute()
        if crew_user.data:
            from datetime import timedelta as _td

            def _fmt_bgw(iso_str: str) -> str:
                if not iso_str:
                    return "—"
                try:
                    utc_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                    bgw_dt = utc_dt + _td(hours=3)
                    return bgw_dt.strftime("%Y-%m-%d  %H:%M")
                except Exception:
                    return iso_str[:16].replace("T", " ")

            def _fmt_utc(iso_str: str) -> str:
                if not iso_str:
                    return "—"
                try:
                    utc_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
                    return utc_dt.strftime("%H:%M UTC")
                except Exception:
                    return iso_str[11:16] + "Z"

            flight_num   = flight.get("flight_number", "")
            origin       = flight.get("origin_code", "")
            dest         = flight.get("destination_code", "")
            dep_iso      = flight.get("departure_time", "")
            arr_iso      = flight.get("arrival_time", "")
            dur_h        = flight.get("duration_hours", 0)
            aircraft     = flight.get("aircraft_type") or flight.get("aircraft_reg") or ""
            crew_name    = crew.get("full_name_ar") or crew.get("full_name_en") or ""
            assigner     = (current_user.get("name_ar")
                            or current_user.get("name_en")
                            or current_user.get("email", ""))

            dep_bgw      = _fmt_bgw(dep_iso)
            arr_bgw      = _fmt_bgw(arr_iso)
            dep_utc      = _fmt_utc(dep_iso)

            message_ar = (
                f"رحلة {flight_num}  ({origin} → {dest})\n"
                f"الإقلاع (بغداد): {dep_bgw}\n"
                f"الوصول (بغداد): {arr_bgw}\n"
                f"الإقلاع (UTC): {dep_utc}\n"
                f"المدة: {dur_h:g}h"
                f"{'  ·  الطائرة: ' + aircraft if aircraft else ''}\n"
                f"بواسطة: {assigner}"
            )
            message_en = (
                f"Flight {flight_num}  ({origin} → {dest})\n"
                f"Departure (Baghdad): {dep_bgw}\n"
                f"Arrival (Baghdad):   {arr_bgw}\n"
                f"Departure (UTC):     {dep_utc}\n"
                f"Duration: {dur_h:g}h"
                f"{'  ·  Aircraft: ' + aircraft if aircraft else ''}\n"
                f"Assigned by: {assigner}"
            )

            crew_user_id = crew_user.data[0]["id"]
            title_ar = f"تم تكليفك برحلة {flight_num}"
            title_en = f"You're assigned to flight {flight_num}"

            sb.table("notifications").insert({
                "id":                   str(uuid.uuid4()),
                "user_id":              crew_user_id,
                "target_user_id":       crew_user_id,
                "company_id":           current_user["company_id"],
                "type":                 "crew_assigned",
                "title_ar":             title_ar,
                "title_en":             title_en,
                "message_ar":           message_ar,
                "message_en":           message_en,
                "body_ar":              message_ar,
                "body_en":              message_en,
                "reference_id":         flight_id,
                "reference_type":       "flight",
                "related_flight_id":    flight_id,
                "related_crew_id":      crew_id,
                "is_read":              False,
                "requires_acknowledge": True,
                "is_acknowledged":      False,
                "created_at":           datetime.now(timezone.utc).isoformat(),
                "updated_at":           datetime.now(timezone.utc).isoformat(),
            }).execute()
            logger.info("Notified crew %s about assignment to flight %s", crew_name, flight_num)

            # Best-effort push to the crew's mobile device(s)
            try:
                push_service.send_to_users(
                    sb, [crew_user_id],
                    title=title_ar,
                    body=f"{flight_num}  ({origin} → {dest})  ·  {dep_bgw}",
                    data={
                        "type":           "crew_assigned",
                        "reference_id":   str(flight_id),
                        "reference_type": "flight",
                    },
                )
            except Exception as pe:
                logger.warning("Push send failed for crew %s: %s", crew_name, pe)
    except Exception as e:
        logger.warning("Notification send failed for assignment to crew %s: %s", crew_id, e)

    return saved


@router.delete("/{assignment_id}")
async def remove_assignment(assignment_id: str, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("assignments").select("id,flight_id").eq("id", assignment_id).execute()
    if not existing.data:
        raise NotFoundError("Assignment", assignment_id)

    # Verify the assignment's flight belongs to this company
    flight_id = existing.data[0].get("flight_id")
    if flight_id:
        flight_check = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
        if not flight_check.data:
            raise NotFoundError("Assignment", assignment_id)

    sb.table("assignments").delete().eq("id", assignment_id).execute()
    return {"message": "Assignment removed successfully", "success": True}


@router.get("/flight/{flight_id}")
async def get_flight_assignments(flight_id: str, current_user: CurrentUser, sb: SbClient):
    flight_check = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_check.data:
        raise NotFoundError("Flight", flight_id)
    result = sb.table("assignments").select("*, crew(full_name_ar, full_name_en, rank, employee_id)").eq("flight_id", flight_id).execute()
    return result.data


@router.post("/{assignment_id}/acknowledge")
async def acknowledge_assignment(assignment_id: str, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("assignments").select("id,flight_id").eq("id", assignment_id).execute()
    if not existing.data:
        raise NotFoundError("Assignment", assignment_id)

    # Verify the assignment's flight belongs to this company
    flight_id = existing.data[0].get("flight_id")
    if flight_id:
        flight_check = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
        if not flight_check.data:
            raise NotFoundError("Assignment", assignment_id)

    result = sb.table("assignments").update({
        "acknowledged": True,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", assignment_id).execute()
    return result.data[0] if result.data else {}
