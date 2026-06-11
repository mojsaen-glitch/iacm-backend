import json
import logging
import re
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


# ── Aircraft registration (REG / tail number) ────────────────────────────────
# REG is the aircraft tail (e.g. YI-ASU) — NOT the flight number (IA-361) and NOT
# the aircraft type (A320). It is MANDATORY: a flight cannot be created, nor can
# its roster be published / finalised, without a valid REG, because the official
# General Declaration (GD) prints it as the aircraft's identity for that flight.
# Format requires a dashed nationality prefix (e.g. YI-ASU, G-ABCD) so an
# aircraft TYPE (A320 / B737 — no dash) can never be mistaken for a tail. Lenient
# on the country prefix to allow foreign / charter tails; Iraqi tails are YI-*.
_REG_RE = re.compile(r"^[A-Z]{1,2}-[A-Z0-9]{2,5}$")


def _normalize_reg(value) -> str:
    return str(value or "").strip().upper()


def _validate_reg_format(reg: str) -> None:
    if not _REG_RE.match(reg):
        raise HTTPException(
            status_code=422,
            detail="صيغة رقم تسجيل الطائرة (REG) غير صحيحة — مثال: YI-ASU",
        )


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
    from_dt: Optional[str] = Query(None, alias="from",
        description="ISO datetime — only flights departing at/after this"),
    to_dt: Optional[str] = Query(None, alias="to",
        description="ISO datetime — only flights departing before this"),
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
    # Optional departure-time window (used by the OCC board to fetch ONLY a given
    # day's flights server-side, instead of paging through historical rows).
    if from_dt:
        query = query.gte("departure_time", from_dt)
    if to_dt:
        query = query.lt("departure_time", to_dt)

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

    # REG (aircraft tail) is mandatory — a flight can't be saved without it.
    reg = _normalize_reg(data.get("aircraft_registration"))
    if not reg:
        raise HTTPException(
            status_code=422,
            detail="رقم تسجيل الطائرة (REG) مطلوب — لا يمكن حفظ الرحلة بدونه.",
        )
    _validate_reg_format(reg)
    data["aircraft_registration"] = reg

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

    # REG can be set/corrected here (movement / ops edit + tail swap). When the
    # caller includes it, it must be a valid, non-empty tail — REG cannot be
    # cleared once a flight has one.
    if "aircraft_registration" in data:
        reg = _normalize_reg(data.get("aircraft_registration"))
        if not reg:
            raise HTTPException(
                status_code=422,
                detail="رقم تسجيل الطائرة (REG) لا يمكن أن يكون فارغاً.",
            )
        _validate_reg_format(reg)
        data["aircraft_registration"] = reg

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

    # REG gate: a flight cannot be published without an aircraft registration —
    # the GD (and the crew's official duty document) require the tail number.
    if not _normalize_reg(flight.get("aircraft_registration")):
        raise HTTPException(
            status_code=422,
            detail="لا يمكن نشر الرحلة بدون رقم تسجيل الطائرة (REG). أضِف REG أولاً.",
        )

    # NOTE: publishing only OPENS a flight for crew assignment — it may legally
    # have no crew yet. The minimum-crew (under-staffing) gate is enforced later,
    # at roster finalisation (see POST /flights/{id}/finalize-roster).

    # Update flight
    updated = sb.table("flights").update({
        "publish_status": "published",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", flight_id).execute()

    # ── Audit: publishing notifies a whole crew — first-class governance event. ──
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "publish_flight", "entity_type": "flight", "entity_id": flight_id,
            "company_id": current_user["company_id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "before_data": json.dumps(
                {"publish_status": flight.get("publish_status") or "draft"}),
            "after_data": json.dumps({
                "publish_status": "published",
                "flight_number": flight.get("flight_number"),
            }, ensure_ascii=False),
        }).execute()
    except Exception as e:
        log.warning("audit_log write failed for publish_flight: %s", e)

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
    (pulls it out of the assignable pool). Idempotent if already a draft.

    SUPERVISORY gate (tighter than publish): un-publishing HIDES the flight
    from crew who were already notified, so specialty schedulers (sched_*) may
    publish but only scheduling/ops supervisors may revert."""
    if current_user.get("role") not in _ROSTER_APPROVERS and not current_user.get("is_superuser"):
        raise ForbiddenError("إلغاء النشر إجراء إشرافي — مدير الجدولة/العمليات فقط")
    flight_res = sb.table("flights").select("*") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    if flight.get("publish_status") != "published":
        return flight  # already a draft — nothing to do

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = sb.table("flights").update({
        "publish_status": "draft",
        "updated_at": now_iso,
    }).eq("id", flight_id).execute()

    fnum = flight.get("flight_number", "")
    origin = flight.get("origin_code", "")
    dest = flight.get("destination_code", "")

    # ── Audit: un-publishing HIDES a flight crew were already notified about. ──
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "unpublish_flight", "entity_type": "flight", "entity_id": flight_id,
            "company_id": current_user["company_id"], "created_at": now_iso,
            "before_data": json.dumps({"publish_status": "published"}),
            "after_data": json.dumps({
                "publish_status": "draft", "flight_number": fnum,
            }, ensure_ascii=False),
        }).execute()
    except Exception as e:
        log.warning("audit_log write failed for unpublish_flight: %s", e)

    # ── Notify the ASSIGNED crew — the flight disappears from their portal, so
    #    it must never vanish silently. Assignments are NOT touched. ──
    try:
        asg = (sb.table("assignments").select("crew_id")
               .eq("flight_id", flight_id).execute().data) or []
        crew_ids = [a["crew_id"] for a in asg if a.get("crew_id")]
        if crew_ids:
            urows = (sb.table("users").select("id,crew_id")
                     .eq("company_id", current_user["company_id"])
                     .in_("crew_id", crew_ids).execute().data) or []
            uids = [u["id"] for u in urows if u.get("id")]
            title_ar = f"سُحب نشر الرحلة {fnum}"
            msg_ar = (f"تم سحب نشر الرحلة {fnum} ({origin} → {dest}) من بوابة الطاقم، "
                      f"يرجى مراجعة الجدولة أو انتظار التحديث.")
            if uids:
                sb.table("notifications").insert([{
                    "id": str(uuid.uuid4()), "user_id": uid, "type": "flight_unpublished",
                    "title_ar": title_ar, "title_en": f"Flight {fnum} unpublished",
                    "message_ar": msg_ar,
                    "message_en": f"Flight {fnum} ({origin}→{dest}) was withdrawn from the "
                                  f"crew portal — await an updated schedule.",
                    "body_ar": msg_ar, "body_en": f"Flight {fnum} unpublished",
                    "reference_id": flight_id, "reference_type": "flight",
                    "related_flight_id": flight_id, "is_read": False, "created_at": now_iso,
                } for uid in uids]).execute()
                push_service.send_to_users(sb, uids, title=title_ar, body=msg_ar,
                                           data={"type": "flight_unpublished",
                                                 "reference_id": str(flight_id),
                                                 "reference_type": "flight"})
    except Exception as e:
        log.warning("unpublish crew-notify failed: %s", e)

    return updated.data[0] if updated.data else flight


# Who may finalise / approve a flight's roster and send it to crew. Movement
# only DEFINES flights; final crew approval stays with scheduling/ops.
_ROSTER_APPROVERS = {
    "super_admin", "admin", "ops_manager", "scheduler", "scheduler_admin",
}

# Who may view a flight's notification READ-receipts (who read what / when).
# Scheduling family + allocators + compliance (read) + movement (read). NEVER crew.
_RECEIPT_VIEWERS = {
    "super_admin", "admin", "ops_manager", "scheduler", "scheduler_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "compliance_officer", "flight_movement", "flight_movement_admin",
}


@router.get("/{flight_id}/assignment-acceptance")
async def flight_assignment_acceptance(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Crew-acceptance board for a flight's roster: who ACCEPTED / is still
    pending / declined / was admin-confirmed. Distinct from read-receipts —
    reading a notification is never acceptance. Crew role is never allowed."""
    if current_user.get("role") not in _RECEIPT_VIEWERS and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بعرض حالة موافقات الطاقم")
    cid = current_user["company_id"]

    fres = sb.table("flights").select(
        "id, flight_number, origin_code, destination_code, departure_time") \
        .eq("id", flight_id).eq("company_id", cid).execute()
    if not fres.data:
        raise NotFoundError("Flight", flight_id)
    flight = fres.data[0]

    rows = (sb.table("assignments").select("*")
            .eq("flight_id", flight_id).execute().data) or []
    crew_ids = [r.get("crew_id") for r in rows if r.get("crew_id")]
    names: dict = {}
    if crew_ids:
        for c in (sb.table("crew").select("id, full_name_ar, full_name_en, rank")
                  .in_("id", crew_ids).execute().data) or []:
            names[c["id"]] = c

    counts = {"accepted": 0, "pending_acceptance": 0, "declined": 0,
              "admin_confirmed": 0}
    items = []
    for r in rows:
        st = _acceptance_status_of(r)
        counts[st] = counts.get(st, 0) + 1
        c = names.get(r.get("crew_id"), {})
        items.append({
            "assignment_id": r.get("id"),
            "crew_id": r.get("crew_id"),
            "crew_name": c.get("full_name_ar") or c.get("full_name_en") or "—",
            "crew_role": r.get("assigned_role") or c.get("rank") or "",
            "duty_type": r.get("duty_type") or "operating",
            "acceptance_status": st,
            "assigned_at": r.get("created_at"),
            "accepted_at": r.get("acknowledged_at") if r.get("acknowledged") else None,
            "declined_at": r.get("declined_at"),
            "response_note": r.get("decline_reason"),
            "admin_confirmed_by": r.get("admin_confirmed_by"),
            "admin_confirmed_at": r.get("admin_confirmed_at"),
            "admin_confirm_reason": r.get("admin_confirm_reason"),
        })
    items.sort(key=lambda i: {"declined": 0, "pending_acceptance": 1,
                              "admin_confirmed": 2, "accepted": 3}
               .get(i["acceptance_status"], 9))

    total = len(items)
    ok = counts["accepted"] + counts["admin_confirmed"]
    return {
        "flight_id": flight_id,
        "flight_number": flight.get("flight_number") or "",
        "route": f"{flight.get('origin_code') or ''}-{flight.get('destination_code') or ''}",
        "departure_time": flight.get("departure_time"),
        "summary": {
            "total": total,
            "accepted": counts["accepted"],
            "pending": counts["pending_acceptance"],
            "declined": counts["declined"],
            "admin_confirmed": counts["admin_confirmed"],
            "removed": 0,   # removals delete the row; the trail lives in audit_log
            "acceptance_percentage": round(ok * 100 / total) if total else 0,
        },
        "items": items,
    }


@router.get("/{flight_id}/notification-receipts")
async def flight_notification_receipts(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Read-receipts board for a flight's notifications: who was notified, who
    READ (is_read/read_at from the notifications table — a push being delivered
    is NOT counted as read), who hasn't yet. Source of truth = notifications
    rows linked to this flight, so it covers publish/unpublish/assignment/GD/
    disruption alike. Company-scoped; crew role is never allowed."""
    if current_user.get("role") not in _RECEIPT_VIEWERS and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بعرض إيصالات إشعارات الرحلة")
    cid = current_user["company_id"]

    fres = sb.table("flights").select(
        "id, flight_number, origin_code, destination_code, departure_time,"
        "publish_status, roster_finalized_status, gd_status") \
        .eq("id", flight_id).eq("company_id", cid).execute()
    if not fres.data:
        raise NotFoundError("Flight", flight_id)
    flight = fres.data[0]

    # Notifications are linked to a flight via related_flight_id (most writers)
    # or reference_id (older writers) — merge both, de-duplicated by id.
    rows: dict[str, dict] = {}
    for col in ("related_flight_id", "reference_id"):
        try:
            for n in (sb.table("notifications").select("*")
                      .eq(col, flight_id).execute().data) or []:
                if n.get("id"):
                    rows[n["id"]] = n
        except Exception as e:
            log.warning("receipts query on %s failed: %s", col, e)
    notifs = sorted(rows.values(), key=lambda n: n.get("created_at") or "", reverse=True)

    # Resolve recipients: user → (role, crew link) → crew (name/rank/phone).
    user_ids = list({n.get("user_id") for n in notifs if n.get("user_id")})
    users_by: dict = {}
    if user_ids:
        for i in range(0, len(user_ids), 500):
            for u in (sb.table("users").select("id, crew_id, role, name_ar, name_en")
                      .in_("id", user_ids[i:i + 500]).execute().data) or []:
                users_by[u["id"]] = u
    crew_ids = list({u.get("crew_id") for u in users_by.values() if u.get("crew_id")})
    crew_by: dict = {}
    if crew_ids:
        for c in (sb.table("crew").select(
                "id, full_name_ar, full_name_en, rank, primary_phone, phone")
                .in_("id", crew_ids).execute().data) or []:
            crew_by[c["id"]] = c

    items = []
    total_read = 0
    last_read = None
    for n in notifs:
        u = users_by.get(n.get("user_id"), {})
        c = crew_by.get(u.get("crew_id"), {})
        is_read = bool(n.get("is_read"))
        if is_read:
            total_read += 1
            ra = n.get("read_at")
            if ra and (last_read is None or ra > last_read):
                last_read = ra
        items.append({
            "notification_id": n.get("id"),
            "notification_type": n.get("type") or "",
            "notification_title": n.get("title_ar") or n.get("title_en") or "",
            "sent_at": n.get("created_at"),
            "read_at": n.get("read_at"),
            "is_read": is_read,
            "status": "read" if is_read else "unread",
            "user_id": n.get("user_id"),
            "recipient_kind": "crew" if u.get("crew_id") else "staff",
            "crew_id": u.get("crew_id"),
            "crew_name": (c.get("full_name_ar") or c.get("full_name_en")
                          or u.get("name_ar") or u.get("name_en") or ""),
            "crew_role": c.get("rank") or u.get("role") or "",
            "crew_rank": c.get("rank") or "",
            "crew_phone": c.get("primary_phone") or c.get("phone") or "",
        })

    total = len(items)
    # Best-effort audit — a supervisor viewing read-receipts is itself auditable.
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "view_flight_notification_receipts",
            "entity_type": "flight", "entity_id": flight_id, "company_id": cid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "after_data": json.dumps({"flight_number": flight.get("flight_number"),
                                      "total": total}, ensure_ascii=False),
        }).execute()
    except Exception as e:
        log.warning("receipts audit failed: %s", e)

    return {
        "flight_id": flight_id,
        "flight_number": flight.get("flight_number") or "",
        "route": f"{flight.get('origin_code') or ''}-{flight.get('destination_code') or ''}",
        "departure_time": flight.get("departure_time"),
        "publish_status": flight.get("publish_status") or "draft",
        "roster_finalized_status": flight.get("roster_finalized_status") or "",
        "gd_status": flight.get("gd_status") or "",
        "summary": {
            "total_sent": total,
            "total_read": total_read,
            "total_unread": total - total_read,
            "read_percentage": round(total_read * 100 / total) if total else 0,
            "last_read_at": last_read,
        },
        "items": items,
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

    # ── Idempotency: already finalised AND not stale → no-op. A flight whose
    # crew changed after approval is marked gd_status='stale' (see
    # mark_gd_stale_if_finalized); re-finalising it is allowed so the GD can be
    # regenerated for the updated roster. ──
    if flight.get("roster_finalized_status") == "finalized" \
            and flight.get("gd_status") != "stale":
        return {
            "ok": True, "flight_id": flight_id, "already_finalized": True,
            "crew_notified": 0, "notified_crew_ids": [],
            "finalized_at": flight.get("roster_finalized_at"),
            "finalized_by": flight.get("roster_finalized_by"),
            "gd_status": flight.get("gd_status"),
        }

    # ── REG gate: no final approval without the aircraft tail (it's the
    # aircraft's identity on the GD that goes to the crew). ──
    if not _normalize_reg(flight.get("aircraft_registration")):
        raise HTTPException(
            status_code=422,
            detail="لا يمكن اعتماد الجدول النهائي بدون رقم تسجيل الطائرة (REG).",
        )

    # ── Minimum-crew (under-staffing) gate ────────────────────────
    shortfalls = _min_crew_shortfalls(sb, flight)
    if shortfalls:
        raise HTTPException(
            status_code=422,
            detail="Minimum crew requirement not met — لم يكتمل الحد الأدنى للطاقم: "
                   + "، ".join(shortfalls),
        )

    # ── Crew-acceptance gate: the FINAL roster needs every operating crew to
    #    have explicitly ACCEPTED (or be admin-confirmed by a supervisor).
    #    pending_acceptance / declined block — reading a notification is NOT
    #    acceptance. ──
    blockers = _acceptance_blockers(sb, flight_id)
    if blockers:
        listing = "؛ ".join(
            f"{b['crew_name']} ({b['rank']}) — "
            f"{'رفض' if b['status'] == 'declined' else 'بانتظار الموافقة'}"
            for b in blockers)
        try:
            sb.table("audit_log").insert({
                "user_id": current_user["id"],
                "user_name": current_user.get("name_ar") or current_user.get("name_en")
                             or current_user.get("email", ""),
                "action": "finalize_blocked_due_to_pending_acceptance",
                "entity_type": "flight", "entity_id": flight_id,
                "company_id": current_user["company_id"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "after_data": json.dumps({
                    "flight_number": flight.get("flight_number"),
                    "blockers": [{"crew_id": b["row"].get("crew_id"),
                                  "crew_name": b["crew_name"],
                                  "status": b["status"]} for b in blockers],
                }, ensure_ascii=False),
            }).execute()
        except Exception as e:
            log.warning("finalize-blocked audit failed: %s", e)
        raise HTTPException(
            status_code=422,
            detail="لا يمكن اعتماد الجدول النهائي لأن بعض أفراد الطاقم لم يوافقوا "
                   "بعد أو رفضوا التكليف: " + listing,
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
    gd_version = (flight.get("gd_version") or 0) + 1
    try:
        sb.table("flights").update({
            "roster_finalized_status": "finalized",
            "roster_finalized_at": now,
            "roster_finalized_by": current_user["id"],
            # GD becomes downloadable by Flight Ops the moment the roster is
            # finalised; version bumps so each (re)finalise is a new GD revision.
            "gd_status": "ready",
            "gd_version": gd_version,
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
                "gd_version": gd_version,
            }, ensure_ascii=False),
            "company_id": current_user["company_id"],
            "created_at": now,
        }).execute()
    except Exception as e:
        log.warning("audit_log insert failed for finalize_roster: %s", str(e)[:200])

    # ── Tell Flight Ops the GD is ready to download. Best-effort — a notify
    # failure must never undo a successful finalisation. ──
    try:
        _notify_flight_ops_gd_ready(sb, current_user["company_id"], flight)
    except Exception as e:
        log.warning("flight-ops GD-ready notify failed for %s: %s", flight_id, e)

    return {
        "ok": True, "flight_id": flight_id, "already_finalized": False,
        "crew_notified": len(user_ids), "notified_crew_ids": notify_crew_ids,
        "finalized_at": now, "finalized_by": current_user["id"],
        "gd_status": "ready", "gd_version": gd_version,
    }


# ── GD (General Declaration) workflow — state + Flight-Ops notifications ──────
# GD is GENERATED client-side (the official form lives in the Flutter app so
# passport data never leaves the device). The server only tracks STATE: a flight
# becomes gd_status='ready' on finalise, and 'stale' if its crew changes after.
# Flight Ops are notified at each transition and download the official file
# (gated to finalised flights) from their portal.
_GD_NOTIFY_ROLES = (
    "flight_operations", "flight_operations_admin", "ops_manager", "super_admin",
)


def _flight_label(flight: dict) -> tuple[str, str, str]:
    """(flight_number, 'ORIG-DEST', 'YYYY-MM-DD') for notification text."""
    num = flight.get("flight_number", "")
    route = f"{flight.get('origin_code', '')}-{flight.get('destination_code', '')}"
    dep = (flight.get("departure_time") or "")[:10]
    return num, route, dep


def _insert_role_notifications(sb, company_id: str, roles, ntype: str,
                               title_ar: str, title_en: str,
                               msg_ar: str, msg_en: str, flight_id) -> int:
    """Fan out one in-app (+ best-effort push) notification to every active user
    whose role is in [roles]. Returns how many were notified."""
    users = (sb.table("users").select("id,role")
             .eq("company_id", company_id).eq("is_active", True).execute().data) or []
    targets = [u["id"] for u in users if u.get("role") in roles]
    if not targets:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    sb.table("notifications").insert([{
        "id": str(uuid.uuid4()), "user_id": uid, "type": ntype,
        "title_ar": title_ar, "title_en": title_en,
        "message_ar": msg_ar, "message_en": msg_en,
        "body_ar": msg_ar, "body_en": msg_en,
        "reference_id": flight_id, "reference_type": "flight",
        "related_flight_id": flight_id,
        "is_read": False, "created_at": now,
    } for uid in targets]).execute()
    try:
        push_service.send_to_users(sb, targets, title=title_ar, body=msg_ar,
                                   data={"type": ntype, "reference_id": str(flight_id),
                                         "reference_type": "flight"})
    except Exception:
        pass  # push is best-effort; the in-app notification is already saved
    return len(targets)


def _notify_flight_ops_gd_ready(sb, company_id: str, flight: dict) -> int:
    num, route, dep = _flight_label(flight)
    return _insert_role_notifications(
        sb, company_id, _GD_NOTIFY_ROLES, "gd_ready",
        "GD جاهز للتحميل", "GD ready to download",
        f"تم اكتمال واعتماد طاقم الرحلة {num} لمسار {route} بتاريخ {dep}. ملف GD جاهز للتحميل.",
        f"Roster for flight {num} ({route}) on {dep} is finalised. The GD file is ready to download.",
        flight.get("id"),
    )


def _acceptance_status_of(row: dict) -> str:
    """Derived acceptance state of an assignment row.
    declined > admin_confirmed > accepted > pending_acceptance."""
    if row.get("declined"):
        return "declined"
    if row.get("admin_confirmed"):
        return "admin_confirmed"
    if row.get("acknowledged"):
        return "accepted"
    return "pending_acceptance"


def _acceptance_blockers(sb, flight_id: str) -> list[dict]:
    """OPERATING crew whose acceptance blocks final approval: still
    pending_acceptance or declined. accepted / admin_confirmed pass.
    Non-operating riders (deadhead/standby/observer/training) never block."""
    rows = (sb.table("assignments").select("*")
            .eq("flight_id", flight_id).execute().data) or []
    out = []
    for r in rows:
        if (r.get("duty_type") or "operating") != "operating":
            continue
        st = _acceptance_status_of(r)
        if st in ("pending_acceptance", "declined"):
            out.append({"row": r, "status": st})
    if not out:
        return []
    crew_ids = [b["row"].get("crew_id") for b in out if b["row"].get("crew_id")]
    names: dict = {}
    if crew_ids:
        for c in (sb.table("crew").select("id, full_name_ar, full_name_en, rank")
                  .in_("id", crew_ids).execute().data) or []:
            names[c["id"]] = c
    for b in out:
        c = names.get(b["row"].get("crew_id"), {})
        b["crew_name"] = c.get("full_name_ar") or c.get("full_name_en") or "—"
        b["rank"] = b["row"].get("assigned_role") or c.get("rank") or ""
    return out


def _gd_blocking_reasons(sb, flight: dict) -> list[str]:
    """Why a flight's official GD cannot be produced (empty = OK to generate)."""
    reasons: list[str] = []
    if not _normalize_reg(flight.get("aircraft_registration")):
        reasons.append("رقم تسجيل الطائرة (REG) غير متوفر")
    asg = (sb.table("assignments").select("crew_id")
           .eq("flight_id", flight["id"]).execute().data) or []
    if not [a for a in asg if a.get("crew_id")]:
        reasons.append("لا يوجد طاقم معيّن")
    reasons.extend(_min_crew_shortfalls(sb, flight))
    for b in _acceptance_blockers(sb, flight["id"]):
        reasons.append(f"{b['crew_name']} "
                       f"({'رفض التكليف' if b['status'] == 'declined' else 'لم يوافق بعد'})")
    return reasons


def mark_gd_stale_if_finalized(sb, company_id: str, flight_id: str, actor: Optional[dict] = None) -> None:
    """If a FINALISED flight's crew changed, flag its GD 'stale' + alert Flight
    Ops that it needs re-approval. Best-effort: never raises into the caller
    (a crew add/remove must not fail because of this side effect)."""
    log = logging.getLogger(__name__)
    try:
        res = (sb.table("flights").select("*")
               .eq("id", flight_id).eq("company_id", company_id).execute())
        if not res.data:
            return
        flight = res.data[0]
        if flight.get("roster_finalized_status") != "finalized":
            return  # never finalised → nothing to invalidate
        if flight.get("gd_status") == "stale":
            return  # already flagged
        now = datetime.now(timezone.utc).isoformat()
        sb.table("flights").update({"gd_status": "stale", "updated_at": now}) \
            .eq("id", flight_id).execute()
        num, route, _ = _flight_label(flight)
        _insert_role_notifications(
            sb, company_id, _GD_NOTIFY_ROLES, "gd_stale",
            "GD يحتاج إعادة اعتماد", "GD needs re-approval",
            f"تغيّر طاقم الرحلة {num} ({route}) بعد الاعتماد — يلزم إعادة اعتماد/توليد ملف GD.",
            f"Crew of flight {num} ({route}) changed after finalisation — GD needs re-approval/regeneration.",
            flight_id,
        )
        try:
            audit = {
                "action": "gd_marked_stale", "entity_type": "flight",
                "entity_id": flight_id, "company_id": company_id, "created_at": now,
                "after_data": json.dumps({"flight_number": flight.get("flight_number", "")},
                                         ensure_ascii=False),
            }
            if actor:
                audit["user_id"] = actor.get("id")
                audit["user_name"] = (actor.get("name_ar") or actor.get("name_en")
                                      or actor.get("email", ""))
            sb.table("audit_log").insert(audit).execute()
        except Exception:
            pass
    except Exception as e:
        log.warning("mark_gd_stale_if_finalized failed for %s: %s", flight_id, e)


@router.post("/{flight_id}/gendec/regenerate")
async def regenerate_gendec(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Re-approve a STALE (crew-changed) finalised flight so its GD is current
    again. Re-runs the same gates as finalise (REG + minimum crew + has crew),
    bumps gd_version, and re-alerts Flight Ops — WITHOUT re-spamming the crew
    (use finalize-roster for the first approval)."""
    if current_user.get("role") not in _ROSTER_APPROVERS and not current_user.get("is_superuser"):
        raise ForbiddenError("إعادة توليد GD يتطلب صلاحية مجدول/مشرف")
    res = (sb.table("flights").select("*")
           .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute())
    if not res.data:
        raise NotFoundError("Flight", flight_id)
    flight = res.data[0]
    if "gd_status" not in flight:
        raise HTTPException(status_code=503,
                            detail="GD migration missing — يلزم تشغيل ترحيل gd_status")
    if flight.get("roster_finalized_status") != "finalized":
        raise HTTPException(status_code=409,
                            detail="الرحلة غير معتمدة بعد — استخدم اعتماد الجدول النهائي أولاً.")
    reasons = _gd_blocking_reasons(sb, flight)
    if reasons:
        raise HTTPException(status_code=422, detail="تعذّر توليد GD: " + "، ".join(reasons))

    now = datetime.now(timezone.utc).isoformat()
    gd_version = (flight.get("gd_version") or 0) + 1
    sb.table("flights").update({
        "gd_status": "ready", "gd_version": gd_version, "updated_at": now,
    }).eq("id", flight_id).execute()
    try:
        _notify_flight_ops_gd_ready(sb, current_user["company_id"], flight)
    except Exception as e:
        logging.getLogger(__name__).warning("GD-ready notify (regenerate) failed: %s", e)
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "gd_regenerated", "entity_type": "flight", "entity_id": flight_id,
            "company_id": current_user["company_id"], "created_at": now,
            "after_data": json.dumps({"gd_version": gd_version}, ensure_ascii=False),
        }).execute()
    except Exception:
        pass
    return {"ok": True, "flight_id": flight_id, "gd_status": "ready", "gd_version": gd_version}


@router.post("/{flight_id}/gendec/log-download")
async def log_gendec_download(flight_id: str, current_user: CurrentUser, sb: SbClient,
                              data: Optional[dict] = Body(default=None)):
    """Audit a GD download (the file is generated client-side). Records who
    downloaded which format so the official document has a paper trail (#10)."""
    fmt = str((data or {}).get("format") or "pdf").lower()
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "gd_downloaded", "entity_type": "flight", "entity_id": flight_id,
            "company_id": current_user["company_id"],
            "after_data": json.dumps({"format": fmt}, ensure_ascii=False),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logging.getLogger(__name__).warning("gd download audit failed: %s", e)
    return {"ok": True}


# ── OCC: notify a flight's crew (a SAFE operational action) ──────────────────
# Sends a free-text message to the crew on a flight. It does NOT change the
# flight, the assignments, or FDP — it only creates notifications + an audit row.
_CREW_NOTIFIERS = _PUBLISH_TOGGLERS | {
    "flight_operations", "flight_operations_admin", "flight_ops",
}
# Operations staff who get a copy when target == 'crew_ops'.
_OPS_NOTIFY_ROLES = {
    "super_admin", "admin", "ops_manager",
    "flight_operations", "flight_operations_admin", "flight_ops",
    "flight_movement", "flight_movement_admin",
}
_NOTIFY_SEVERITIES = {"info", "warning", "urgent"}
_SEVERITY_PREFIX = {"info": "", "warning": "⚠ ", "urgent": "🔴 "}


@router.post("/{flight_id}/notify-crew")
async def notify_flight_crew(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    if current_user.get("role") not in _CREW_NOTIFIERS and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بإرسال إشعارات للطاقم")

    flight_res = sb.table("flights").select("*") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    message = str(data.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="نص الرسالة مطلوب")
    severity = str(data.get("severity") or "info").lower()
    if severity not in _NOTIFY_SEVERITIES:
        severity = "info"
    target = str(data.get("target") or "all_crew").lower()  # all_crew | captain | crew_ops

    asg = (sb.table("assignments").select("crew_id, duty_type")
           .eq("flight_id", flight_id).execute().data) or []
    if target == "captain":
        crew_ids = [a["crew_id"] for a in asg if a.get("crew_id")]
        if crew_ids:
            ranks = (sb.table("crew").select("id,rank").in_("id", crew_ids).execute().data) or []
            crew_ids = [r["id"] for r in ranks if is_captain_rank(r.get("rank"))]
    else:  # all_crew | crew_ops → the operating crew
        crew_ids = [a["crew_id"] for a in asg
                    if a.get("crew_id") and (a.get("duty_type") or "operating") == "operating"]

    user_ids: set = set()
    if crew_ids:
        urows = (sb.table("users").select("id,crew_id")
                 .eq("company_id", current_user["company_id"])
                 .in_("crew_id", crew_ids).execute().data) or []
        user_ids.update(u["id"] for u in urows if u.get("id"))

    # 'crew_ops' → also send a copy to the OCC / operations staff.
    if target == "crew_ops":
        ops_rows = (sb.table("users").select("id,role")
                    .eq("company_id", current_user["company_id"]).eq("is_active", True)
                    .execute().data) or []
        user_ids.update(u["id"] for u in ops_rows if u.get("role") in _OPS_NOTIFY_ROLES)
    user_ids = list(user_ids)

    default_title = {"info": "رسالة عمليات", "warning": "تنبيه عمليات", "urgent": "رسالة عاجلة"}[severity]
    title = (str(data.get("title") or "").strip()) or default_title
    title = _SEVERITY_PREFIX[severity] + title
    flight_num = flight.get("flight_number", "")
    now = datetime.now(timezone.utc).isoformat()

    if user_ids:
        sb.table("notifications").insert([{
            "id": str(uuid.uuid4()), "user_id": uid,
            "type": "occ_message",
            "title_ar": title, "title_en": title,
            "message_ar": message, "message_en": message,
            "body_ar": message, "body_en": message,
            "reference_id": flight_id, "reference_type": "flight",
            "related_flight_id": flight_id,
            "is_read": False, "created_at": now,
        } for uid in user_ids]).execute()
        try:
            push_service.send_to_users(
                sb, user_ids, title=f"{title} — {flight_num}", body=message,
                data={"type": "occ_message", "reference_id": str(flight_id),
                      "reference_type": "flight"})
        except Exception as pe:
            logging.getLogger(__name__).warning("notify-crew push failed: %s", pe)

    # Audit — record WHO sent WHAT to whom (no flight/assignment change).
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "occ_notify_crew", "entity_type": "flight", "entity_id": flight_id,
            "company_id": current_user["company_id"], "created_at": now,
            "after_data": json.dumps({
                "flight_number": flight_num, "severity": severity, "target": target,
                "recipients": len(user_ids), "title": title,
            }, ensure_ascii=False),
        }).execute()
    except Exception as e:
        logging.getLogger(__name__).warning("notify-crew audit failed: %s", e)

    return {"ok": True, "sent": len(user_ids), "severity": severity, "target": target}


# ── OCC: delay a flight (first flight-data-CHANGING OCC action) ──────────────
# Sets status='delayed' + delay_minutes (ETD = STD + delay; STD itself is NOT
# touched). Optionally notifies crew/ops. Always audited.
@router.post("/{flight_id}/delay")
async def delay_flight(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    if current_user.get("role") not in _FLIGHT_EDITORS and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بتأخير الرحلة")

    flight_res = sb.table("flights").select("*") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not flight_res.data:
        raise NotFoundError("Flight", flight_id)
    flight = flight_res.data[0]

    # Derive the delay (minutes) from a new ETD or an explicit delay_minutes.
    # STD (departure_time) is preserved.
    try:
        std = datetime.fromisoformat(str(flight.get("departure_time")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="الموعد المجدول للرحلة غير صالح")

    new_etd = data.get("new_etd")
    if new_etd:
        try:
            etd = datetime.fromisoformat(str(new_etd).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="صيغة وقت ETD غير صحيحة")
        dm = int(round((etd - std).total_seconds() / 60))
    elif data.get("delay_minutes") is not None:
        try:
            dm = int(data["delay_minutes"])
        except (TypeError, ValueError):
            dm = 0
    else:
        raise HTTPException(status_code=422, detail="وقت ETD الجديد مطلوب")
    if dm <= 0:
        raise HTTPException(status_code=422, detail="يجب أن يكون وقت ETD بعد الموعد المجدول")

    reason = str(data.get("reason") or "").strip().lower()
    if reason and reason not in CANCELLATION_REASONS:
        raise HTTPException(status_code=422,
                            detail=f"سبب التأخير يجب أن يكون أحد: {', '.join(sorted(CANCELLATION_REASONS))}")
    note = str(data.get("note") or "").strip()

    now = datetime.now(timezone.utc).isoformat()
    update = {"status": "delayed", "delay_minutes": dm, "updated_at": now}
    if reason:
        update["cancellation_reason"] = reason   # column reused for delay reason
    if note:
        update["cancellation_notes"] = note[:500]
    sb.table("flights").update(update).eq("id", flight_id).execute()

    # ── Notify (optional) crew and/or operations. ──
    notify_crew = bool(data.get("notify_crew", True))
    notify_ops = bool(data.get("notify_ops", False))
    fnum = flight.get("flight_number", "")
    origin = flight.get("origin_code", "")
    dest = flight.get("destination_code", "")
    title_ar = f"تأخّر الرحلة {fnum}"
    title_en = f"Flight {fnum} delayed"
    msg_ar = f"رحلة {fnum} ({origin} → {dest}) تأخّرت {dm} دقيقة" + (f" — {note}" if note else "")
    msg_en = f"Flight {fnum} ({origin} → {dest}) delayed {dm} min" + (f" — {note}" if note else "")
    targets: set = set()
    if notify_crew:
        asg = (sb.table("assignments").select("crew_id, duty_type")
               .eq("flight_id", flight_id).execute().data) or []
        crew_ids = [a["crew_id"] for a in asg
                    if a.get("crew_id") and (a.get("duty_type") or "operating") == "operating"]
        if crew_ids:
            urows = (sb.table("users").select("id,crew_id")
                     .eq("company_id", current_user["company_id"])
                     .in_("crew_id", crew_ids).execute().data) or []
            targets.update(u["id"] for u in urows if u.get("id"))
    if notify_ops:
        ops = (sb.table("users").select("id,role")
               .eq("company_id", current_user["company_id"]).eq("is_active", True)
               .execute().data) or []
        targets.update(u["id"] for u in ops if u.get("role") in _OPS_NOTIFY_ROLES)
    targets = list(targets)
    if targets:
        try:
            sb.table("notifications").insert([{
                "id": str(uuid.uuid4()), "user_id": uid, "type": "flight_disruption",
                "title_ar": title_ar, "title_en": title_en,
                "message_ar": msg_ar, "message_en": msg_en,
                "body_ar": msg_ar, "body_en": msg_en,
                "reference_id": flight_id, "reference_type": "flight",
                "related_flight_id": flight_id, "is_read": False, "created_at": now,
            } for uid in targets]).execute()
            push_service.send_to_users(sb, targets, title=title_ar, body=msg_ar,
                                       data={"type": "flight_disruption",
                                             "reference_id": str(flight_id),
                                             "reference_type": "flight"})
        except Exception as e:
            logging.getLogger(__name__).warning("delay notify failed: %s", e)

    # ── Audit. ──
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "occ_delay_flight", "entity_type": "flight", "entity_id": flight_id,
            "company_id": current_user["company_id"], "created_at": now,
            "after_data": json.dumps({
                "flight_number": fnum, "delay_minutes": dm,
                "reason": reason or None, "note": note or None,
                "recipients": len(targets),
            }, ensure_ascii=False),
        }).execute()
    except Exception as e:
        logging.getLogger(__name__).warning("delay audit failed: %s", e)

    return {"ok": True, "flight_id": flight_id, "status": "delayed",
            "delay_minutes": dm, "recipients": len(targets)}


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
