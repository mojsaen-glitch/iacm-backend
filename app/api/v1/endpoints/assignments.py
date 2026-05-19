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

# ── Role gates ────────────────────────────────────────────────────────────────
# Anyone in `_READERS` may browse the full company-wide assignment list.
# A logged-in `crew` member is allowed too, but their query is force-narrowed
# to their own crew_id (see _ensure_assignment_reader below).
_READERS = {
    "super_admin", "admin", "ops_manager", "scheduler",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "compliance_officer", "flight_movement", "flight_ops", "flight_operations",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}
# Only these roles can create / mutate assignments. Allocator sub-rank gates
# below still apply to limit which crew they can pick.
_ASSIGNERS = {
    "super_admin", "admin", "ops_manager", "scheduler",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}


def _ensure_assignment_reader(user: dict) -> Optional[str]:
    """Return the crew_id the response must be scoped to, or None for full read.

    Raises ForbiddenError if the role is not allowed to view assignments at all.
    """
    role = user.get("role")
    if role in _READERS:
        return None
    if role == "crew":
        own = user.get("crew_id")
        if not own:
            raise ForbiddenError("Crew account is not linked to a roster record")
        return own
    raise ForbiddenError("غير مصرح بعرض التعيينات")


def _ensure_assigner(user: dict) -> None:
    if user.get("role") not in _ASSIGNERS:
        raise ForbiddenError("غير مصرح بتعيين الطاقم")


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
    forced_crew_id = _ensure_assignment_reader(current_user)
    company_id = current_user["company_id"]

    q = sb.table("assignments") \
        .select("*, flights!inner(*)") \
        .eq("flights.company_id", company_id)
    if flight_id:
        q = q.eq("flight_id", flight_id)
    # For `crew` role, force-narrow to their own crew_id regardless of what
    # they passed. For ops staff, honour the optional filter.
    if forced_crew_id is not None:
        q = q.eq("crew_id", forced_crew_id)
    elif crew_id:
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
    # Top-level role gate. Allocator-rank limits (below) still apply, but
    # without this gate anyone holding a token could create assignments.
    _ensure_assigner(current_user)
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
    # Removing an assignment is a scheduling action — same gate as creating
    # one. Crew should use /decline if they cannot fly; deletion erases the
    # audit trail and must stay with ops.
    _ensure_assigner(current_user)
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
    # Crew can only call this for a flight they themselves are on. Ops staff
    # see the whole roster for the flight.
    forced_crew_id = _ensure_assignment_reader(current_user)
    flight_check = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_check.data:
        raise NotFoundError("Flight", flight_id)
    if forced_crew_id is not None:
        own_row = sb.table("assignments").select("id").eq("flight_id", flight_id).eq("crew_id", forced_crew_id).limit(1).execute()
        if not own_row.data:
            raise ForbiddenError("غير مصرح بعرض طاقم رحلة لست ضمنها")
    result = sb.table("assignments").select("*, crew(full_name_ar, full_name_en, rank, employee_id)").eq("flight_id", flight_id).execute()
    return result.data


@router.post("/{assignment_id}/acknowledge")
async def acknowledge_assignment(assignment_id: str, current_user: CurrentUser, sb: SbClient):
    existing = sb.table("assignments").select("id,flight_id,crew_id").eq("id", assignment_id).execute()
    if not existing.data:
        raise NotFoundError("Assignment", assignment_id)

    row = existing.data[0]

    # Verify the assignment's flight belongs to this company
    flight_id = row.get("flight_id")
    if flight_id:
        flight_check = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
        if not flight_check.data:
            raise NotFoundError("Assignment", assignment_id)

    # Crew can only acknowledge their own row. Ops staff (admin / ops_manager /
    # scheduler) can ack on behalf of crew when, e.g., they get verbal
    # confirmation in the OCC. Any other role is rejected outright.
    role = current_user.get("role")
    if role == "crew":
        if current_user.get("crew_id") != row.get("crew_id"):
            raise ForbiddenError("Cannot acknowledge another crew member's assignment")
    elif role not in {"super_admin", "admin", "ops_manager", "scheduler"}:
        raise ForbiddenError("غير مصرح بتأكيد التعيين")

    result = sb.table("assignments").update({
        "acknowledged": True,
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", assignment_id).execute()
    return result.data[0] if result.data else {}


@router.post("/{assignment_id}/decline")
async def decline_assignment(
    assignment_id: str, data: dict, current_user: CurrentUser, sb: SbClient
):
    """Crew declines an assignment with a reason.

    Marks the row as declined + notifies every scheduler/ops manager in the
    company so the row can be reassigned quickly. The scheduler then chooses
    a replacement; the declined row stays on the audit trail.
    """
    existing = sb.table("assignments").select("id,flight_id,crew_id").eq("id", assignment_id).execute()
    if not existing.data:
        raise NotFoundError("Assignment", assignment_id)

    row = existing.data[0]
    flight_id = row.get("flight_id")
    reason = (data.get("reason") or "").strip()

    # Verify scope + capture flight number for the notification body.
    flight_number = "—"
    if flight_id:
        f = sb.table("flights").select("flight_number,company_id")\
            .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
        if not f.data:
            raise NotFoundError("Assignment", assignment_id)
        flight_number = f.data[0].get("flight_number", "—")

    # Crew can only decline their own row.
    if current_user.get("role") == "crew" and current_user.get("crew_id") != row.get("crew_id"):
        raise ForbiddenError("Cannot decline another crew member's assignment")

    sb.table("assignments").update({
        "acknowledged": False,
        "declined": True,
        "decline_reason": reason or None,
        "declined_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", assignment_id).execute()

    # Fan-out alert to schedulers + ops managers
    targets = sb.table("users").select("id")\
        .eq("company_id", current_user["company_id"])\
        .in_("role", ["admin", "super_admin", "ops_manager", "scheduler"])\
        .execute()
    now_iso = datetime.now(timezone.utc).isoformat()
    crew_name = current_user.get("name_ar") or current_user.get("name_en") or "طاقم"
    rows = [{
        "id":             str(uuid.uuid4()),
        "user_id":        u["id"],
        "type":           "assignment_declined",
        "title_ar":       "رفض تكليف",
        "title_en":       "Assignment declined",
        "message_ar":     f"{crew_name} رفض رحلة {flight_number}"
                          + (f" — السبب: {reason}" if reason else ""),
        "message_en":     f"{crew_name} declined flight {flight_number}"
                          + (f" — reason: {reason}" if reason else ""),
        "reference_id":   assignment_id,
        "reference_type": "assignment",
        "is_read":        False,
        "created_at":     now_iso,
    } for u in (targets.data or [])]
    if rows:
        sb.table("notifications").insert(rows).execute()

    return {"declined": True, "notified": len(rows)}


@router.post("/crew-self-report", status_code=201)
async def file_crew_self_report(
    data: dict, current_user: CurrentUser, sb: SbClient
):
    """Crew files a fatigue or sick report.

    Body: { type: 'fatigue' | 'sick', notes?: str }

    Logs as a notification routed to every scheduler/ops manager so they
    can act (remove from upcoming pairings, schedule replacement). The
    crew member is the only one who can file on their own behalf —
    schedulers don't create these for someone else.
    """
    report_type = (data.get("type") or "").strip().lower()
    if report_type not in {"fatigue", "sick"}:
        raise HTTPException(status_code=422, detail="type must be 'fatigue' or 'sick'")
    if current_user.get("role") != "crew":
        raise ForbiddenError("Only crew can file fatigue or sick reports for themselves")

    notes = (data.get("notes") or "").strip()
    targets = sb.table("users").select("id")\
        .eq("company_id", current_user["company_id"])\
        .in_("role", ["admin", "super_admin", "ops_manager", "scheduler"])\
        .execute()
    crew_name = current_user.get("name_ar") or current_user.get("name_en") or "طاقم"
    title_ar  = "تقرير إجهاد" if report_type == "fatigue" else "إعلان مرضي"
    title_en  = "Fatigue report" if report_type == "fatigue" else "Sick report"
    now_iso   = datetime.now(timezone.utc).isoformat()
    body_ar   = f"{crew_name}" + (f" — {notes}" if notes else "")

    rows = [{
        "id":             str(uuid.uuid4()),
        "user_id":        u["id"],
        "type":           f"crew_{report_type}_report",
        "title_ar":       title_ar,
        "title_en":       title_en,
        "message_ar":     body_ar,
        "message_en":     body_ar,
        "reference_id":   current_user.get("crew_id"),
        "reference_type": "crew",
        "is_read":        False,
        "created_at":     now_iso,
    } for u in (targets.data or [])]
    if rows:
        sb.table("notifications").insert(rows).execute()

    return {"type": report_type, "notified": len(rows)}
