import json
import logging
import uuid, math
from typing import Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Query, HTTPException, Body
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ForbiddenError
from app.core.fleet_complement import (
    category_for_rank, is_captain_rank, min_required_for_category,
)
from app.services import push_service

router = APIRouter(prefix="/flights", tags=["Flights"])

# Only operations staff create / modify flights. Movement dept needs to
# create flights on their movement portal, so they're included.
_FLIGHT_EDITORS = {
    "super_admin", "admin", "ops_manager", "scheduler", "flight_movement",
    # Department admin of the movement division has its members' capabilities.
    "flight_movement_admin",
}


def _ensure_flight_editor(user: dict) -> None:
    if user.get("role") not in _FLIGHT_EDITORS:
        raise ForbiddenError("Only ops / scheduling / movement can create or modify flights")


# Publishing / un-publishing a roster is a SCHEDULING action — broader than
# editing a flight's definition (times/route). The whole scheduling division may
# toggle a flight's publish state, on top of the flight editors above. Creating
# or modifying the flight ITSELF stays restricted to _FLIGHT_EDITORS.
_PUBLISH_TOGGLERS = _FLIGHT_EDITORS | {
    "scheduler_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}


def _ensure_publisher(user: dict) -> None:
    if user.get("role") not in _PUBLISH_TOGGLERS:
        raise ForbiddenError("غير مصرح بنشر أو إلغاء نشر الرحلات")


# ── Automatic time-based status lifecycle ────────────────────────────────────
# A published flight advances on its own as wall-clock time crosses its
# (delay-adjusted) departure / arrival timestamps:
#   scheduled → boarding (T-30m) → departed (at dep) → arrived (at arr)
# This runs lazily whenever the flight list is fetched (the apps poll it), so
# no always-on cron is required. Each transition fans out an in-app + push
# notification to the crew assigned to that flight.
_BOARDING_LEAD = timedelta(minutes=30)
_ADVANCEABLE = ("scheduled", "boarding", "departed")

# Throttle: cap the auto-advance scan to at most once per company per window so
# heavy polling can't trigger it on every request. A real scheduler (VPS cron
# hitting POST /flights/advance-statuses) is the proper trigger at scale; this
# throttle just bounds the cost while it also runs lazily on the list path.
_ADVANCE_THROTTLE = timedelta(seconds=60)
_last_advance_at: dict[str, datetime] = {}
_STATUS_LABELS = {
    "boarding": ("بدء الصعود", "Boarding started"),
    "departed": ("أقلعت الرحلة", "Flight departed"),
    "arrived":  ("وصلت الرحلة", "Flight arrived"),
}


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _due_status(flight: dict, now: datetime) -> Optional[str]:
    """Return the status this flight SHOULD be at `now`, or None if unchanged."""
    delay = timedelta(minutes=flight.get("delay_minutes") or 0)
    dep = _parse_ts(flight.get("departure_time"))
    arr = _parse_ts(flight.get("arrival_time"))
    if arr and now >= arr + delay:
        return "arrived"
    if dep and now >= dep + delay:
        return "departed"
    if dep and now >= dep + delay - _BOARDING_LEAD:
        return "boarding"
    return None


def _advance_due_statuses(sb, company_id: str) -> None:
    """Advance published flights to their time-due status and notify crew.

    Best-effort and idempotent: once a flight's status is updated it no longer
    matches the candidate query, so it can't be re-notified.
    """
    now = datetime.now(timezone.utc)
    res = sb.table("flights").select(
        "id,flight_number,status,departure_time,arrival_time,"
        "delay_minutes,origin_code,destination_code"
    ).eq("company_id", company_id) \
     .eq("publish_status", "published") \
     .in_("status", list(_ADVANCEABLE)).execute()

    transitions = []  # (flight, new_status)
    for f in (res.data or []):
        new = _due_status(f, now)
        if new and new != f.get("status"):
            transitions.append((f, new))
    if not transitions:
        return

    now_iso = now.isoformat()
    for f, new in transitions:
        sb.table("flights").update({"status": new, "updated_at": now_iso}) \
            .eq("id", f["id"]).execute()

    # Resolve assigned crew → user accounts for notifications.
    flight_ids = [f["id"] for f, _ in transitions]
    assigns = (sb.table("assignments").select("flight_id,crew_id")
               .in_("flight_id", flight_ids).execute().data) or []
    crew_ids = list({a["crew_id"] for a in assigns if a.get("crew_id")})
    crew_to_user = {}
    if crew_ids:
        urs = (sb.table("users").select("id,crew_id")
               .eq("company_id", company_id).eq("is_active", True)
               .in_("crew_id", crew_ids).execute().data) or []
        crew_to_user = {u["crew_id"]: u["id"] for u in urs if u.get("crew_id")}

    notifs = []
    for f, new in transitions:
        fid = f["id"]
        title_ar, title_en = _STATUS_LABELS.get(new, (new, new))
        num = f.get("flight_number", "")
        route = f"{f.get('origin_code', '')}→{f.get('destination_code', '')}"
        msg_ar = f"رحلة {num} ({route}): {title_ar}"
        msg_en = f"Flight {num} ({route}): {title_en}"
        recipients = [crew_to_user[a["crew_id"]] for a in assigns
                      if a.get("flight_id") == fid and a.get("crew_id") in crew_to_user]
        for uid in recipients:
            notifs.append({
                "id": str(uuid.uuid4()),
                "user_id": uid,
                "type": "flight_status",
                "title_ar": title_ar,
                "title_en": title_en,
                "message_ar": msg_ar,
                "message_en": msg_en,
                "reference_id": fid,
                "reference_type": "flight",
                "is_read": False,
                "created_at": now_iso,
            })
    if notifs:
        sb.table("notifications").insert(notifs).execute()
        try:
            push_service.send_to_users(
                sb, [n["user_id"] for n in notifs],
                title="تحديث حالة الرحلة",
                body=notifs[-1]["message_ar"],
                data={"type": "flight_status", "reference_type": "flight"},
            )
        except Exception as pe:
            logging.getLogger(__name__).warning("status push failed: %s", pe)


@router.get("")
async def list_flights(
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    status: Optional[str] = None,
):
    # Lazily advance time-due flight statuses (+ notify crew) before listing,
    # but at most once per company per throttle window so frequent polling
    # can't turn this into a per-request DB storm. Best-effort: never blocks.
    cid = current_user["company_id"]
    now = datetime.now(timezone.utc)
    last = _last_advance_at.get(cid)
    if last is None or now - last >= _ADVANCE_THROTTLE:
        _last_advance_at[cid] = now
        try:
            _advance_due_statuses(sb, cid)
        except Exception as e:
            logging.getLogger(__name__).warning("auto status advance failed: %s", e)

    # estimated count: an exact count re-scans all flights (36k+/year) on every
    # page load; the planner estimate is plenty for a paginated list total.
    query = sb.table("flights").select("*", count="estimated").eq("company_id", current_user["company_id"])
    if status:
        query = query.eq("status", status)

    skip = (page - 1) * page_size
    result = query.order("departure_time", desc=False).range(skip, skip + page_size - 1).execute()
    total = result.count or 0

    return {
        "items": result.data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
    }


@router.post("/advance-statuses")
async def advance_statuses(current_user: CurrentUser, sb: SbClient):
    """Advance time-due flight statuses for ALL companies (+ notify crew).

    This is the PROPER trigger at scale: point a system cron (e.g. on the VPS,
    every 1 min) at this endpoint instead of relying on the lazy list-path run.
    Restricted to admin / ops_manager so a service account can drive it.
    """
    if current_user.get("role") not in ("super_admin", "admin", "ops_manager"):
        raise ForbiddenError("يتطلب صلاحية مدير العمليات")
    # Distinct companies that have advanceable published flights.
    res = sb.table("flights").select("company_id") \
        .eq("publish_status", "published") \
        .in_("status", list(_ADVANCEABLE)).execute()
    companies = {r["company_id"] for r in (res.data or []) if r.get("company_id")}
    advanced = 0
    for cid in companies:
        try:
            _advance_due_statuses(sb, cid)
            _last_advance_at[cid] = datetime.now(timezone.utc)
            advanced += 1
        except Exception as e:
            logging.getLogger(__name__).warning("advance for %s failed: %s", cid, e)
    return {"companies_processed": advanced}


# Roles that plan/assign crew — notified when شعبة الحركة creates a new flight.
_SCHEDULER_NOTIFY_ROLES = (
    "scheduler", "scheduler_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
)


def _notify_schedulers_new_flight(sb, company_id: str, flight: dict) -> None:
    """Fan out an in-app (+ push) notification to every crew scheduler in the
    company telling them a new flight was created and is ready to plan."""
    if not flight:
        return
    flight_id  = flight.get("id")
    flight_num = flight.get("flight_number", "")
    dep        = (flight.get("departure_time") or "")[:16].replace("T", " ")
    origin     = flight.get("origin_code", "")
    dest       = flight.get("destination_code", "")
    msg_ar = f"تم إنشاء رحلة جديدة: {flight_num} ({origin}→{dest}) في {dep}"
    msg_en = f"New flight created: {flight_num} ({origin}→{dest}) at {dep}"

    users_res = sb.table("users").select("id,role") \
        .eq("company_id", company_id).eq("is_active", True).execute()
    notifs = [{
        "id":             str(uuid.uuid4()),
        "user_id":        u["id"],
        "type":           "flight_created",
        "title_ar":       "رحلة جديدة",
        "title_en":       "New Flight Created",
        "message_ar":     msg_ar,
        "message_en":     msg_en,
        "reference_id":   flight_id,
        "reference_type": "flight",
        "is_read":        False,
        "created_at":     datetime.now(timezone.utc).isoformat(),
    } for u in (users_res.data or []) if u.get("role") in _SCHEDULER_NOTIFY_ROLES]

    if not notifs:
        return
    sb.table("notifications").insert(notifs).execute()
    try:
        push_service.send_to_users(
            sb, [n["user_id"] for n in notifs],
            title="رحلة جديدة", body=msg_ar,
            data={"type": "flight_created",
                  "reference_id": str(flight_id), "reference_type": "flight"},
        )
    except Exception:
        pass  # push is best-effort; the in-app notification is already saved


@router.post("", status_code=201)
async def create_flight(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_flight_editor(current_user)
    try:
        dep = datetime.fromisoformat(data["departure_time"].replace("Z", "+00:00"))
        arr = datetime.fromisoformat(data["arrival_time"].replace("Z", "+00:00"))
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid datetime format: {e}")
    duration = round((arr - dep).total_seconds() / 3600, 2)

    data["id"] = str(uuid.uuid4())
    data["company_id"] = current_user["company_id"]
    data["duration_hours"] = duration
    data.setdefault("status", "scheduled")
    data.setdefault("publish_status", "draft")
    data.setdefault("crew_required", 4)
    data.setdefault("standby_required", 0)
    # Standby (احتياط) is a non-negative planning count.
    try:
        data["standby_required"] = max(0, int(data["standby_required"]))
    except (TypeError, ValueError):
        data["standby_required"] = 0
    data.setdefault("delay_minutes", 0)
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("flights").insert(data).execute()
    created = result.data[0] if result.data else {}

    # Notify crew schedulers that a new flight was created so they can begin
    # planning crew assignment. Best-effort — never fail the create on notify.
    try:
        _notify_schedulers_new_flight(sb, current_user["company_id"], created)
    except Exception as e:
        logging.getLogger(__name__).warning("create_flight notify failed: %s", e)

    return created


@router.get("/pending-assignment")
async def get_flights_pending_assignment(current_user: CurrentUser, sb: SbClient):
    """
    Returns published flights that still need crew assigned.
    Department allocators see this filtered to their relevance.
    """
    result = sb.table("flights").select("*") \
        .eq("company_id", current_user["company_id"]) \
        .eq("publish_status", "published") \
        .neq("status", "cancelled") \
        .order("departure_time", desc=False).execute()
    return result.data or []


@router.get("/{flight_id}")
async def get_flight(flight_id: str, current_user: CurrentUser, sb: SbClient):
    result = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not result.data:
        raise NotFoundError("Flight", flight_id)
    return result.data[0]


@router.patch("/{flight_id}")
async def update_flight(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_flight_editor(current_user)
    existing = sb.table("flights").select("id").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Flight", flight_id)

    if "departure_time" in data or "arrival_time" in data:
        flight = sb.table("flights").select("departure_time,arrival_time").eq("id", flight_id).execute().data[0]
        dep_str = data.get("departure_time", flight["departure_time"])
        arr_str = data.get("arrival_time", flight["arrival_time"])
        try:
            dep = datetime.fromisoformat(str(dep_str).replace("Z", "+00:00"))
            arr = datetime.fromisoformat(str(arr_str).replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Invalid datetime format: {e}")
        data["duration_hours"] = round((arr - dep).total_seconds() / 3600, 2)

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("flights").update(data).eq("id", flight_id).execute()
    return result.data[0] if result.data else {}


@router.put("/{flight_id}")
async def update_flight_put(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """PUT alias for PATCH — full or partial update."""
    return await update_flight(flight_id, data, current_user, sb)


@router.delete("/{flight_id}", status_code=204)
async def delete_flight(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Delete a flight + cascade to every table that references it.
    Admin / Ops Manager / شعبة الحركة (flight_movement manages its flights)."""
    if current_user["role"] not in (
        "super_admin", "admin", "ops_manager", "flight_movement", "flight_movement_admin",
    ):
        raise ForbiddenError("Admin / Ops Manager / Flight Movement access required")
    existing = sb.table("flights").select("id,flight_number").eq("id", flight_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Flight", flight_id)
    flight_number = existing.data[0].get("flight_number", "")

    log = logging.getLogger(__name__)

    def _try(label: str, fn):
        try:
            fn()
        except Exception as e:
            log.warning("delete_flight (%s) — %s step failed: %s", flight_id, label, e)

    # 1) Assignments — must go first (FK on flights AND referenced by notifications)
    _try("assignments", lambda: sb.table("assignments")
         .delete().eq("flight_id", flight_id).execute())

    # 2) Notifications referencing this flight via either column
    _try("notifications.related_flight_id", lambda: sb.table("notifications")
         .delete().eq("related_flight_id", flight_id).execute())
    _try("notifications.reference_id", lambda: sb.table("notifications")
         .delete().eq("reference_id", flight_id).eq("reference_type", "flight").execute())

    # 3) The flight row itself — surface leftover FK errors as a useful 409
    try:
        sb.table("flights").delete().eq("id", flight_id).execute()
    except Exception as e:
        msg = str(e)
        log.exception("delete_flight (%s) main delete failed", flight_id)
        if "foreign key" in msg.lower() or "violates" in msg.lower():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"تعذّر حذف الرحلة {flight_number}: لا تزال هناك سجلات مرتبطة بها "
                    f"({msg[:120]})"
                ),
            )
        raise HTTPException(status_code=502, detail=f"تعذّر حذف الرحلة: {msg[:200]}")

    # 4) Audit log — best-effort
    try:
        sb.table("audit_log").insert({
            "user_id":     current_user["id"],
            "user_name":   current_user.get("name_ar") or current_user.get("name_en") or current_user["email"],
            "action":      "delete_flight",
            "entity_type": "flight",
            "entity_id":   flight_id,
            "company_id":  current_user["company_id"],
            "before_data": json.dumps({"flight_number": flight_number}),
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.warning("audit_log write failed for delete_flight: %s", e)


@router.post("/{flight_id}/publish")
async def publish_flight(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """
    Publish a flight for crew assignment.
    - Changes publish_status to 'published'
    - Sends in-app notification to all allocators in the same company
    """
    # Publishing puts a flight into the assignable pool and fans out
    # notifications to every allocator — a scheduling action.
    _ensure_publisher(current_user)
    flight_res = sb.table("flights").select("*") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    if flight.get("publish_status") == "published":
        return flight  # already published

    # NOTE: publishing only OPENS a flight for crew assignment — it may legally
    # have no crew yet. The minimum-crew (under-staffing) gate is enforced later,
    # at roster finalisation (see POST /flights/{id}/finalize-roster).

    # Update flight
    updated = sb.table("flights").update({
        "publish_status": "published",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", flight_id).execute()

    # Notify all allocators (cabin + cockpit + ground + crew_allocator)
    allocator_roles = [
        "crew_allocator", "cabin_allocator",
        "cockpit_allocator", "ground_allocator",
        "ops_manager", "admin",
    ]
    users_res = sb.table("users").select("id,role") \
        .eq("company_id", current_user["company_id"]) \
        .eq("is_active", True).execute()

    flight_num = flight.get("flight_number", "")
    dep        = flight.get("departure_time", "")[:16].replace("T", " ")
    origin     = flight.get("origin_code", "")
    dest       = flight.get("destination_code", "")
    msg_ar     = f"رحلة جديدة تحتاج تكليف طاقم: {flight_num} ({origin}→{dest}) في {dep}"
    msg_en     = f"New flight needs crew assignment: {flight_num} ({origin}→{dest}) at {dep}"

    notifs = []
    for u in (users_res.data or []):
        if u["role"] in allocator_roles:
            notifs.append({
                "id":          str(uuid.uuid4()),
                "user_id":     u["id"],
                "type":        "flight_published",
                "title_ar":    "رحلة جديدة للتكليف",
                "title_en":    "New Flight for Assignment",
                "message_ar":  msg_ar,
                "message_en":  msg_en,
                "reference_id": flight_id,
                "reference_type": "flight",
                "is_read":     False,
                "created_at":  datetime.now(timezone.utc).isoformat(),
            })
    if notifs:
        sb.table("notifications").insert(notifs).execute()
        # Best-effort push fan-out to all allocators' mobile devices
        try:
            push_service.send_to_users(
                sb, [n["user_id"] for n in notifs],
                title="رحلة جديدة للتكليف",
                body=msg_ar,
                data={
                    "type":           "flight_published",
                    "reference_id":   str(flight_id),
                    "reference_type": "flight",
                },
            )
        except Exception as pe:
            log = logging.getLogger(__name__)
            log.warning("Push send failed for flight_published: %s", pe)

    # ── Notify every crew member rostered on this flight ──────────────────────
    # Crew are told "تم تكليفك" when the roster is PUBLISHED (not on each draft
    # assignment). Reuses the assignment-notifier so each gets an in-app entry +
    # push + a notification_delivery record. Best-effort — never blocks publish.
    try:
        from app.api.v1.endpoints.assignments import _notify_crew_assigned
        asg = sb.table("assignments").select("crew_id") \
            .eq("flight_id", flight_id).execute()
        crew_ids = {a["crew_id"] for a in (asg.data or []) if a.get("crew_id")}
        for cid in crew_ids:
            try:
                _notify_crew_assigned(sb, cid, flight)
            except Exception:
                logging.getLogger(__name__).warning(
                    "publish notify failed crew=%s flight=%s", cid, flight_id)
    except Exception as e:
        logging.getLogger(__name__).warning("publish crew-notify block failed: %s", e)

    return updated.data[0] if updated.data else flight


@router.post("/{flight_id}/unpublish")
async def unpublish_flight(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Revert a published flight back to draft so its roster can be edited again
    (pulls it out of the assignable pool). Same scheduling gate as publishing;
    idempotent if the flight is already a draft."""
    _ensure_publisher(current_user)
    flight_res = sb.table("flights").select("*") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    if flight.get("publish_status") != "published":
        return flight  # already a draft — nothing to do

    updated = sb.table("flights").update({
        "publish_status": "draft",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", flight_id).execute()
    return updated.data[0] if updated.data else flight


# Who may finalise / approve a flight's roster and send it to crew. Movement
# only DEFINES flights; final crew approval stays with scheduling/ops.
_ROSTER_APPROVERS = {
    "super_admin", "admin", "ops_manager", "scheduler", "scheduler_admin",
}


def _min_crew_shortfalls(sb, flight: dict) -> list[str]:
    """Return human-readable shortfalls if the flight's assigned crew is below
    the safety floor (no captain / too few pilots / too few cabin). Empty list
    means the minimum complement is met. Driven by the fleet spec.

    Non-operating duty types (deadhead / standby / observer / training) RIDE
    the flight but do NOT satisfy the GenDec — they are excluded from the
    count here, so a flight with 3 operating CC + 1 deadhead CC still shows
    a shortfall (3/4) and cannot be finalised."""
    ac_type  = flight.get("aircraft_type")
    flight_id = flight["id"]
    asg = sb.table("assignments").select("crew_id, duty_type") \
        .eq("flight_id", flight_id).execute().data or []
    crew_ids = [r["crew_id"] for r in asg
                if r.get("crew_id") and (r.get("duty_type") or "operating") == "operating"]
    ranks = []
    if crew_ids:
        ranks = [r.get("rank") for r in
                 (sb.table("crew").select("rank").in_("id", crew_ids).execute().data or [])]
    pilots   = sum(1 for r in ranks if category_for_rank(r) == "pilot")
    cabin    = sum(1 for r in ranks if category_for_rank(r) == "cabin")
    captains = sum(1 for r in ranks if is_captain_rank(r))
    need_pilots = min_required_for_category(ac_type, "pilot")
    need_cabin  = min_required_for_category(ac_type, "cabin")

    out: list[str] = []
    if captains < 1:
        out.append("لا يوجد قائد طائرة (Captain)")
    if pilots < need_pilots:
        out.append(f"عدد الطيارين أقل من المطلوب ({pilots}/{need_pilots})")
    if cabin < need_cabin:
        out.append(f"عدد طاقم المقصورة أقل من المطلوب ({cabin}/{need_cabin})")
    return out


@router.post("/{flight_id}/finalize-roster")
async def finalize_roster(flight_id: str, current_user: CurrentUser, sb: SbClient,
                          data: Optional[dict] = Body(default=None)):
    """Approve a flight's crew roster and notify the assigned crew (the final
    'send schedule to crew' step). UNDER-STAFFING GATE lives HERE — a flight
    can be created and published (opened for assignment) with no crew, but its
    roster cannot be finalised until it meets the minimum complement: at least
    one captain, the minimum cockpit crew, and the cabin safety floor.

    IDEMPOTENT: once finalised, a repeat call sends NO new notifications and
    returns `already_finalized: true`. Optional body `skip_notify_crew_ids`
    lets the caller suppress duplicate notifications for crew already notified
    on another sector of the same connected duty."""
    log = logging.getLogger(__name__)
    if current_user.get("role") not in _ROSTER_APPROVERS and not current_user.get("is_superuser"):
        raise ForbiddenError("اعتماد الجدول النهائي يتطلب صلاحية مجدول/مشرف")

    flight_res = sb.table("flights").select("*") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    # ── FAIL-CLOSED: roster-finalise state must be storable, else NO action. ──
    # If the migration hasn't run, `select("*")` won't return these columns. We
    # refuse to finalise (and therefore never notify) when we cannot record that
    # the roster was finalised — otherwise a repeat call would re-spam crew.
    if "roster_finalized_status" not in flight:
        raise HTTPException(
            status_code=503,
            detail="Roster finalize migration is missing — اعتماد الجدول معطّل: "
                   "يلزم تشغيل ترحيل قاعدة البيانات (roster_finalized columns)",
        )

    # ── Idempotency: already finalised → no-op, no new notifications. ──
    if flight.get("roster_finalized_status") == "finalized":
        return {
            "ok": True, "flight_id": flight_id, "already_finalized": True,
            "crew_notified": 0, "notified_crew_ids": [],
            "finalized_at": flight.get("roster_finalized_at"),
            "finalized_by": flight.get("roster_finalized_by"),
        }

    # ── Minimum-crew (under-staffing) gate ────────────────────────
    shortfalls = _min_crew_shortfalls(sb, flight)
    if shortfalls:
        raise HTTPException(
            status_code=422,
            detail="Minimum crew requirement not met — لم يكتمل الحد الأدنى للطاقم: "
                   + "، ".join(shortfalls),
        )

    # Crew on this sector, minus any the caller already notified elsewhere in
    # the same duty (per-crew de-duplication across a connected pairing).
    # Non-operating duty types (deadhead/standby/observer/training) RIDE the
    # flight but are not "assigned" in the operational sense — we don't push
    # them a "you're assigned to flight X" notification.
    skip = set((data or {}).get("skip_notify_crew_ids") or [])
    asg = sb.table("assignments").select("crew_id, duty_type") \
        .eq("flight_id", flight_id).execute().data or []
    crew_ids = [r["crew_id"] for r in asg if r.get("crew_id")]
    notify_crew_ids = [r["crew_id"] for r in asg
                       if r.get("crew_id")
                       and (r.get("duty_type") or "operating") == "operating"
                       and r["crew_id"] not in skip]
    user_ids = []
    if notify_crew_ids:
        urows = sb.table("users").select("id,crew_id") \
            .eq("company_id", current_user["company_id"]) \
            .in_("crew_id", notify_crew_ids).execute().data or []
        user_ids = [u["id"] for u in urows if u.get("id")]

    # ── Persist finalisation state FIRST — if we cannot save it, we abort
    # BEFORE notifying (no notifications without a recorded approval). ──
    now = datetime.now(timezone.utc).isoformat()
    try:
        sb.table("flights").update({
            "roster_finalized_status": "finalized",
            "roster_finalized_at": now,
            "roster_finalized_by": current_user["id"],
            "updated_at": now,
        }).eq("id", flight_id).execute()
    except Exception as e:
        log.exception("roster finalize persist failed for %s", flight_id)
        raise HTTPException(
            status_code=502,
            detail=f"تعذّر حفظ حالة الاعتماد — لم تُرسَل الإشعارات: {str(e)[:200]}",
        )

    # ── State saved → now notify the assigned crew. ──
    # This is the FIRST notification the crew receives for this flight (we no
    # longer notify on assign_crew). Send the full detail block — Baghdad +
    # UTC times, duration, aircraft — so the crew has everything in one place.
    flight_num = flight.get("flight_number", "")
    origin     = flight.get("origin_code", "")
    dest       = flight.get("destination_code", "")
    dep_iso    = flight.get("departure_time", "")
    arr_iso    = flight.get("arrival_time", "")
    dur_h      = flight.get("duration_hours", 0) or 0
    aircraft   = flight.get("aircraft_type") or flight.get("aircraft_reg") or ""

    def _fmt_bgw(iso_str: str) -> str:
        if not iso_str:
            return "—"
        try:
            utc_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            bgw_dt = utc_dt + timedelta(hours=3)
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
            return (iso_str[11:16] + "Z") if len(iso_str) >= 16 else iso_str

    dep_bgw = _fmt_bgw(dep_iso)
    arr_bgw = _fmt_bgw(arr_iso)
    dep_utc = _fmt_utc(dep_iso)

    title_ar = f"تم تكليفك برحلة {flight_num}"
    title_en = f"You're assigned to flight {flight_num}"
    msg_ar = (
        f"رحلة {flight_num}  ({origin} → {dest})\n"
        f"الإقلاع (بغداد): {dep_bgw}\n"
        f"الوصول (بغداد): {arr_bgw}\n"
        f"الإقلاع (UTC): {dep_utc}\n"
        f"المدة: {dur_h:g}h"
        f"{'  ·  الطائرة: ' + aircraft if aircraft else ''}"
    )
    msg_en = (
        f"Flight {flight_num}  ({origin} → {dest})\n"
        f"Departure (Baghdad): {dep_bgw}\n"
        f"Arrival (Baghdad):   {arr_bgw}\n"
        f"Departure (UTC):     {dep_utc}\n"
        f"Duration: {dur_h:g}h"
        f"{'  ·  Aircraft: ' + aircraft if aircraft else ''}"
    )

    if user_ids:
        sb.table("notifications").insert([{
            "id": str(uuid.uuid4()), "user_id": uid,
            "type": "crew_assigned",
            "title_ar": title_ar, "title_en": title_en,
            "message_ar": msg_ar, "message_en": msg_en,
            "body_ar":   msg_ar, "body_en":   msg_en,
            "reference_id": flight_id, "reference_type": "flight",
            "related_flight_id": flight_id,
            "is_read": False,
            "requires_acknowledge": True,
            "is_acknowledged": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        } for uid in user_ids]).execute()
        try:
            push_service.send_to_users(sb, user_ids, title=title_ar,
                                       body=f"{flight_num}  ({origin} → {dest})  ·  {dep_bgw}",
                                       data={"type": "crew_assigned",
                                             "reference_id": str(flight_id),
                                             "reference_type": "flight"})
        except Exception as pe:
            log.warning("Push send failed for finalize-roster notify: %s", pe)

    # ── Audit trail. ──
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "finalize_roster",
            "entity_type": "flight",
            "entity_id": flight_id,
            "after_data": json.dumps({
                "flight_number": flight_num,
                "crew_notified": len(user_ids),
                "crew_total": len(crew_ids),
                "finalized_at": now,
            }, ensure_ascii=False),
            "company_id": current_user["company_id"],
            "created_at": now,
        }).execute()
    except Exception as e:
        log.warning("audit_log insert failed for finalize_roster: %s", str(e)[:200])

    return {
        "ok": True, "flight_id": flight_id, "already_finalized": False,
        "crew_notified": len(user_ids), "notified_crew_ids": notify_crew_ids,
        "finalized_at": now, "finalized_by": current_user["id"],
    }


VALID_STATUSES = {"scheduled", "boarding", "departed", "landed", "cancelled", "diverted", "delayed"}

# Reason codes used by the cancellation / delay dialogs in the UI.
# Keeping the list closed-set so we can build aggregate reports later
# (e.g. "what % of cancellations were weather vs. technical?").
CANCELLATION_REASONS = {
    "weather", "technical", "crew_shortage", "operational",
    "commercial", "atc", "security", "other",
}

@router.patch("/{flight_id}/status")
async def update_flight_status(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Update operational status of a flight (flight movement tracking).

    For status=cancelled or delayed the body may include:
      - `reason`: closed-set reason code (see CANCELLATION_REASONS)
      - `reason_notes`: free-text explanation
      - `delay_minutes`: integer (for status=delayed)
    These get stamped onto the flight row + an audit entry so we can
    build IROPS reports later.
    """
    # شعبة الحركة (flight_movement) tracks operational status — cancel / delay /
    # depart / arrive — so it's allowed here alongside ops management.
    if current_user["role"] not in (
        "super_admin", "admin", "ops_manager", "flight_movement", "flight_movement_admin",
    ):
        raise ForbiddenError("يتطلب صلاحية مدير العمليات أو شعبة الحركة")

    new_status = data.get("status", "").lower()
    if new_status not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status يجب أن يكون أحد القيم التالية: {', '.join(VALID_STATUSES)}"
        )

    existing = sb.table("flights").select("id,flight_number,company_id")\
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Flight", flight_id)

    update = {
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Capture reason on cancel/delay/divert so reports can break down by cause.
    if new_status in {"cancelled", "delayed", "diverted"}:
        reason = (data.get("reason") or "").strip().lower()
        if reason:
            if reason not in CANCELLATION_REASONS:
                raise HTTPException(
                    status_code=422,
                    detail=f"reason must be one of: {', '.join(CANCELLATION_REASONS)}",
                )
            update["cancellation_reason"] = reason  # column reused for delay/divert too
        if data.get("reason_notes"):
            update["cancellation_notes"] = str(data["reason_notes"])[:500]
        if new_status == "delayed" and data.get("delay_minutes") is not None:
            try:
                update["delay_minutes"] = int(data["delay_minutes"])
            except (TypeError, ValueError):
                pass

    result = sb.table("flights").update(update).eq("id", flight_id).execute()

    # ── Notify assigned crew on cancellation / delay ──────────────
    # A disruption that doesn't reach the crew defeats the purpose; fan out a
    # best-effort in-app notification to everyone rostered on this flight.
    if new_status in {"cancelled", "delayed", "diverted"}:
        try:
            fnum = existing.data[0].get("flight_number", "")
            asg = sb.table("assignments").select("crew_id").eq("flight_id", flight_id).execute().data or []
            crew_ids = [a["crew_id"] for a in asg if a.get("crew_id")]
            if crew_ids:
                urows = sb.table("users").select("id,crew_id").in_("crew_id", crew_ids).execute().data or []
                label = {"cancelled": ("أُلغيت", "cancelled"),
                         "delayed":   ("تأخّرت", "delayed"),
                         "diverted":  ("حُوِّلت", "diverted")}[new_status]
                extra = ""
                if new_status == "delayed" and update.get("delay_minutes"):
                    extra = f" ({update['delay_minutes']} د)"
                now_iso = datetime.now(timezone.utc).isoformat()
                for u in urows:
                    sb.table("notifications").insert({
                        "id":             str(uuid.uuid4()),
                        "user_id":        u["id"],
                        "target_user_id": u["id"],
                        "company_id":     current_user["company_id"],
                        "type":           "flight_disruption",
                        "title_ar":       f"رحلة {fnum} {label[0]}",
                        "title_en":       f"Flight {fnum} {label[1]}",
                        "message_ar":     f"رحلتك {fnum} {label[0]}{extra} — راجع جدولك.",
                        "message_en":     f"Your flight {fnum} was {label[1]}{extra} — check your roster.",
                        "body_ar":        f"رحلتك {fnum} {label[0]}{extra} — راجع جدولك.",
                        "body_en":        f"Your flight {fnum} was {label[1]}{extra} — check your roster.",
                        "reference_id":   flight_id,
                        "reference_type": "flight",
                        "related_flight_id": flight_id,
                        "is_read":        False,
                        "created_at":     now_iso,
                        "updated_at":     now_iso,
                    }).execute()
        except Exception as e:
            log.warning("disruption notification failed for flight %s: %s", flight_id, e)

    return result.data[0] if result.data else {}
