import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ConflictError, FTLViolationError, CrewBlockedError, ForbiddenError
from app.core.config import settings
from app.core.compliance_engine import ComplianceEngine, IRAQI_AIRPORTS
from app.core.fleet_complement import (
    category_for_rank, required_for_category,
    operational_expected_by_role, min_required_for_category,
    flight_deck_expected_by_role, cabin_crew_expected_by_role,
)
from app.core.crew_roles import (
    assignment_bucket as role_bucket, is_operational_only, CATEGORY_ORDER,
    role_category, normalize_role, roles_in_categories, expand_with_legacy,
    role_label,
    CAT_FLIGHT_DECK, CAT_CABIN, CAT_TECHNICAL, CAT_GROUND, CAT_SECURITY, CAT_OBSERVER,
)
from app.api.v1.endpoints.incompatibility import get_approved_dnp_pairs
from app.services import push_service

logger = logging.getLogger(__name__)


# Roles alerted immediately when a crew-assignment push fails to deliver.
_PUSH_ALERT_ROLES = (
    "super_admin", "admin", "ops_manager",
    "scheduler", "scheduler_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
)


def _alert_push_failure(sb, company_id: str, flight: dict, flight_num: str,
                        crew_name: str = "") -> None:
    """Immediate in-app alert to ops + schedulers when a crew-assignment push
    fails (FCM token rejected) so they can follow up out-of-band — names the
    crew member whose notification didn't arrive."""
    if not company_id:
        return
    who_ar = crew_name or "أحد أفراد الطاقم"
    who_en = crew_name or "a crew member"
    users = sb.table("users").select("id,role").eq("company_id", company_id) \
        .eq("is_active", True).execute().data or []
    now = datetime.now(timezone.utc).isoformat()
    notifs = [{
        "id":             str(uuid.uuid4()),
        "user_id":        u["id"],
        "type":           "delivery_alert",
        "title_ar":       "تنبيه: فشل إشعار تكليف",
        "title_en":       "Alert: assignment push failed",
        "message_ar":     f"فشل وصول إشعار التكليف (Push) إلى {who_ar} على الرحلة {flight_num}",
        "message_en":     f"Push delivery failed for {who_en} on flight {flight_num}",
        "reference_id":   flight.get("id"),
        "reference_type": "flight",
        "is_read":        False,
        "created_at":     now,
    } for u in users if u.get("role") in _PUSH_ALERT_ROLES]
    if notifs:
        sb.table("notifications").insert(notifs).execute()


def _notify_crew_assigned(sb, crew_id: str, flight: dict) -> None:
    """Notify the assigned crew member + open a notification_delivery record per
    recipient (delivery monitoring). Per recipient: create the in-app
    notification, attempt push, and store the per-user FCM result + status
    (sent | failed). On push failure → immediate alert to ops + schedulers.
    No-op if the crew has no user account. Best-effort throughout."""
    users = sb.table("users").select("id").eq("crew_id", crew_id) \
        .eq("is_active", True).execute().data or []
    if not users:
        return
    flight_id  = flight.get("id")
    company_id = flight.get("company_id")
    flight_num = flight.get("flight_number", "")
    # Crew display name — so a push-failure alert can say WHO it failed for.
    crew_name = ""
    try:
        cr = sb.table("crew").select("full_name_ar,full_name_en,roster_name") \
            .eq("id", crew_id).limit(1).execute().data
        if cr:
            crew_name = (cr[0].get("full_name_ar") or cr[0].get("full_name_en")
                         or cr[0].get("roster_name") or "")
    except Exception:
        pass
    dep        = (flight.get("departure_time") or "")[:16].replace("T", " ")
    origin     = flight.get("origin_code", "")
    dest       = flight.get("destination_code", "")
    msg_ar = f"تم تكليفك برحلة {flight_num} ({origin}→{dest}) في {dep}"
    msg_en = f"You've been assigned to flight {flight_num} ({origin}→{dest}) at {dep}"
    data = {"type": "crew_assigned",
            "reference_id": str(flight_id), "reference_type": "flight"}

    push_failed_any = False
    for u in users:
        now = datetime.now(timezone.utc).isoformat()
        notif_id = str(uuid.uuid4())
        sb.table("notifications").insert({
            "id": notif_id, "user_id": u["id"], "type": "crew_assigned",
            "title_ar": "تم تكليفك برحلة", "title_en": "You've been assigned",
            "message_ar": msg_ar, "message_en": msg_en,
            "reference_id": flight_id, "reference_type": "flight",
            "is_read": False, "created_at": now,
        }).execute()

        # Per-user push → derive this recipient's delivery status + FCM result.
        try:
            res = push_service.send_to_users(
                sb, [u["id"]], title="تم تكليفك برحلة", body=msg_ar, data=data)
        except Exception:
            res = {"attempted": 0, "succeeded": 0, "failed": 0}
        res = res or {}
        attempted = res.get("attempted", 0)
        succeeded = res.get("succeeded", 0)
        if attempted == 0:
            status, fcm_result = "sent", "no_token"       # in-app only; ACK via poll
        elif res.get("stub"):
            # FCM not configured on the server (no service account) — the push
            # was never really attempted, so this is NOT a delivery failure.
            status, fcm_result = "sent", "push_stub"
        elif succeeded > 0:
            status, fcm_result = "sent", f"push_ok {succeeded}/{attempted}"
        else:
            status, fcm_result = "failed", f"push_failed 0/{attempted}"
            push_failed_any = True

        # Delivery record — best-effort (table may be absent on older envs).
        try:
            sb.table("notification_delivery").insert({
                "id": str(uuid.uuid4()), "notification_id": notif_id,
                "user_id": u["id"], "crew_id": crew_id, "flight_id": flight_id,
                "status": status, "fcm_result": fcm_result,
                "sent_at": now, "created_at": now, "updated_at": now,
            }).execute()
        except Exception as e:
            logger.warning("notification_delivery insert failed: %s", e)

    if push_failed_any:
        try:
            _alert_push_failure(sb, company_id, flight, flight_num, crew_name)
        except Exception as e:
            logger.warning("push-failure alert failed: %s", e)
router = APIRouter(prefix="/assignments", tags=["Crew Assignments"])

# Legal values for assignments.duty_type — mirrors the DB CHECK constraint in
# migrations/2026_06_15_assignment_duty_type.sql. Only 'operating' counts
# toward the GenDec complement, per-role cap, and minimum-crew publish gate;
# the rest (deadhead / standby / observer / training) ride the flight but
# never satisfy a missing role.
_DUTY_TYPES = frozenset({
    "operating", "deadhead", "standby", "observer", "training",
})
_OPERATING = "operating"

# Specialty scheduler → the crew ROLES it may see & assign. Keys are the new
# GenDec role values; legacy crew.rank is normalised (normalize_role) before the
# check, so a sched_captain matches both 'pilot_captain' and legacy 'captain'.
# The general "scheduler" role is intentionally NOT here — it stays unrestricted.
SCHED_ALLOWED_ROLES = {
    "sched_captain":  frozenset({"pilot_captain"}),
    "sched_copilot":  frozenset({"pilot_first_officer"}),
    "sched_engineer": frozenset({"aircraft_maintenance_engineer", "technical_staff"}),
    "sched_purser":   frozenset({"senior_cabin_crew"}),
    "sched_cabin":    frozenset({"cabin_crew"}),
    "sched_balance":  frozenset({"load_sheet_officer"}),
    "sched_security": frozenset({"in_flight_security_officer", "security_staff"}),
    "sched_extra":    frozenset({"observer"}),
}

# Broad allocators → the crew CATEGORIES they may assign.
ALLOC_ALLOWED_CATEGORIES = {
    "cockpit_allocator": frozenset({CAT_FLIGHT_DECK}),
    "cabin_allocator":   frozenset({CAT_CABIN}),
    # Ground allocator covers ALL operational sections (technical/ground/security/observer).
    "ground_allocator":  frozenset({CAT_TECHNICAL, CAT_GROUND, CAT_SECURITY, CAT_OBSERVER}),
}


def _role_may_assign_rank(user_role: str, crew_rank: str) -> bool:
    """Whether a specialty scheduler / broad allocator may assign this crew rank.
    General scheduler / admins aren't in either map → unrestricted (True).
    Legacy and new crew.rank values both resolve via the role registry."""
    allowed_roles = SCHED_ALLOWED_ROLES.get(user_role)
    if allowed_roles is not None:
        return normalize_role(crew_rank) in allowed_roles
    allowed_cats = ALLOC_ALLOWED_CATEGORIES.get(user_role)
    if allowed_cats is not None:
        return role_category(crew_rank) in allowed_cats
    return True  # not a restricted role

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
    # Department admins view their division's operational pages (read-only).
    "flight_movement_admin", "scheduler_admin",
    "flight_operations_admin", "compliance_admin",
}
# Only these roles can create / mutate assignments. Allocator sub-rank gates
# below still apply to limit which crew they can pick.
# NOTE: flight_movement is intentionally NOT here — شعبة الحركة only DEFINES the
# required crew composition on a flight (e.g. 1 captain + 1 engineer + N cabin),
# it does NOT assign actual crew members. Assignment stays with schedulers.
_ASSIGNERS = {
    "super_admin", "admin", "ops_manager", "scheduler",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
    # Scheduling-division admin oversees the schedulers, so it can assign too.
    "scheduler_admin",
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


def _restricted_ranks(user: dict):
    """Crew ROLES this user may see/assign, or None for all. Specialty
    schedulers → their exact roles; broad allocators → every role in their
    allowed categories. Registry-driven (new GenDec roles)."""
    role = user.get("role", "")
    sched = SCHED_ALLOWED_ROLES.get(role)
    if sched is not None:
        return expand_with_legacy(sched)
    cats = ALLOC_ALLOWED_CATEGORIES.get(role)
    if cats is not None:
        return expand_with_legacy(roles_in_categories(cats))
    return None


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

    # Enrich each row with crew identity so callers can render + GROUP the
    # roster (flight_deck / cabin_crew / ground_operations) without extra
    # round-trips. `assignment_type` (bucket) + `assigned_role` already live on
    # the row; we add the crew name, the canonical rank, and whether the crew
    # member has a (login) account.
    crew_ids = list({r["crew_id"] for r in output if r.get("crew_id")})
    if crew_ids:
        crew_map = {c["id"]: c for c in (sb.table("crew")
            .select("id, full_name_ar, full_name_en, rank, status")
            .in_("id", crew_ids).execute().data or [])}
        acct_ids = {u["crew_id"] for u in (sb.table("users")
            .select("crew_id").in_("crew_id", crew_ids).eq("is_active", True)
            .execute().data or []) if u.get("crew_id")}
        for r in output:
            c = crew_map.get(r.get("crew_id"), {})
            r["crew_name_ar"] = c.get("full_name_ar", "")
            r["crew_name_en"] = c.get("full_name_en", "")
            # `rank` kept for backward-compat (older callers read a['rank']);
            # prefer the rank captured at assignment time when present.
            r["rank"] = r.get("assigned_role") or c.get("rank", "")
            r["crew_status"] = c.get("status", "")
            r["has_account"] = r.get("crew_id") in acct_ids
            # Self-heal: if a row has no valid roster-section bucket, derive it
            # from the rank now (covers legacy 'regular'/'connected' + the old
            # 3-bucket values that predate the 6-section model).
            if r.get("assignment_type") not in CATEGORY_ORDER:
                r["assignment_type"] = _bucket_for_rank(r.get("rank"))

    return output


@router.get("/flight/{flight_id}/roster")
async def get_flight_roster(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Crew on a flight, GROUPED into the 6 roster sections + per-section
    expected counts (the GenDec template for this aircraft type).

    Response shape (backward-compatible — legacy section keys remain top-level):
        {
          "flight_deck":          [...],
          "cabin_crew":           [...],
          "technical_operations": [...],
          "ground_operations":    [...],
          "flight_security":      [...],
          "observer":             [...],
          "_meta": {
             "aircraft_type": "B737",
             "expected": {
                "flight_deck":  {"min": 2, "is_counted": true},
                "cabin_crew":   {"min": 4, "is_counted": true},
                "technical_operations": {"min": 1, "is_counted": false,
                                         "by_role": {"aircraft_maintenance_engineer": 1,
                                                     "technical_staff": 0}},
                ...
             }
          }
        }

    `is_counted=true` → missing crew BLOCKS publish (hard gate, existing behavior).
    `is_counted=false` → missing crew is ADVISORY only (warning badge, never blocks).

    A crew member may only read the roster of a flight they're assigned to.
    """
    forced_crew_id = _ensure_assignment_reader(current_user)
    fl = sb.table("flights").select("id, aircraft_type, departure_time, arrival_time") \
        .eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
    if not fl.data:
        raise NotFoundError("Flight", flight_id)
    aircraft_type = (fl.data[0].get("aircraft_type") or "").upper()
    # Block time (hours) — needed to decide cockpit augment on wide-body.
    block_h: float | None = None
    try:
        dep = fl.data[0].get("departure_time")
        arr = fl.data[0].get("arrival_time")
        if dep and arr:
            d = datetime.fromisoformat(str(dep).replace("Z", "+00:00"))
            a = datetime.fromisoformat(str(arr).replace("Z", "+00:00"))
            block_h = max((a - d).total_seconds() / 3600.0, 0.0)
    except Exception:                                  # robust to bad timestamps
        block_h = None

    rows = sb.table("assignments").select("*").eq("flight_id", flight_id).execute().data or []
    groups: dict[str, list] = {c: [] for c in CATEGORY_ORDER}

    # A crew user can only see rosters of flights they're on.
    if rows and forced_crew_id is not None and \
            forced_crew_id not in {r.get("crew_id") for r in rows}:
        raise ForbiddenError("لا يمكنك عرض طاقم رحلة لست مكلّفاً بها")

    # Non-operating crew (deadhead / standby / observer / training) — RIDE the
    # flight but never satisfy a missing role. Returned in a separate block so
    # the UI can render them outside the GenDec counter.
    non_operating: dict[str, list] = {
        "deadhead": [], "standby": [], "observer": [], "training": [],
    }

    if rows:
        crew_ids = list({r["crew_id"] for r in rows if r.get("crew_id")})
        crew_map = {c["id"]: c for c in (sb.table("crew")
            .select("id, full_name_ar, full_name_en, rank")
            .in_("id", crew_ids).execute().data or [])}
        acct_ids = {u["crew_id"] for u in (sb.table("users")
            .select("crew_id").in_("crew_id", crew_ids).eq("is_active", True)
            .execute().data or []) if u.get("crew_id")}

        for r in rows:
            c = crew_map.get(r.get("crew_id"), {})
            rank = r.get("assigned_role") or c.get("rank", "")
            duty = (r.get("duty_type") or _OPERATING).lower()
            entry = {
                "assignment_id": r.get("id"),
                "crew_id":       r.get("crew_id"),
                "name_ar":       c.get("full_name_ar", ""),
                "name_en":       c.get("full_name_en", ""),
                "rank":          rank,
                "duty_type":     duty,
                "has_account":   r.get("crew_id") in acct_ids,
            }
            # Non-operating rows go to their own block ONLY — they never appear
            # in the GenDec sections, so the per-role counter stays clean.
            if duty != _OPERATING:
                bucket = duty if duty in non_operating else "deadhead"
                non_operating[bucket].append(entry)
                continue
            bucket = r.get("assignment_type")
            if bucket not in groups:                      # self-heal legacy rows
                bucket = _bucket_for_rank(rank)
            groups[bucket].append(entry)

    # ── Per-section expected complement (the GenDec template) ────────────────
    # Counted sections (flight_deck/cabin_crew) use the safety-floor minimums.
    # Operational sections use the per-aircraft operational template.
    op_by_role = operational_expected_by_role(aircraft_type)
    # Aggregate operational expectations per section.
    op_per_section: dict[str, dict[str, int]] = {
        CAT_TECHNICAL: {}, CAT_GROUND: {}, CAT_SECURITY: {}, CAT_OBSERVER: {},
    }
    for role_key, expected in op_by_role.items():
        cat = role_category(role_key)
        if cat in op_per_section:
            op_per_section[cat][role_key] = expected

    fd_by_role = flight_deck_expected_by_role(aircraft_type, block_h)
    cc_by_role = cabin_crew_expected_by_role(aircraft_type)
    expected: dict[str, dict] = {
        CAT_FLIGHT_DECK: {
            "min": sum(fd_by_role.values()),
            "is_counted": True,
            "by_role": fd_by_role,
        },
        CAT_CABIN: {
            "min": sum(cc_by_role.values()),
            "is_counted": True,
            "by_role": cc_by_role,
        },
    }
    for cat in (CAT_TECHNICAL, CAT_GROUND, CAT_SECURITY, CAT_OBSERVER):
        by_role = op_per_section[cat]
        expected[cat] = {
            "min": sum(by_role.values()),
            "is_counted": False,
            "by_role": by_role,
        }

    out = dict(groups)
    out["non_operating"] = non_operating
    out["_meta"] = {
        "aircraft_type": aircraft_type,
        "expected": expected,
        "counting_rule": "Only operating assignments count toward GenDec complement",
    }
    return out


def _is_overridable_block(rule: str) -> bool:
    """Which BLOCKING compliance rules a supervisor override may bypass.

    ONLY the administratively-overridable Flight-Time-Limit family — rest, FDP
    and accumulated flight hours. Everything else (time conflict / double
    booking, crew blocked-suspended, missing aircraft type rating, expired
    safety documents / training, and any compliance-engine self-error) is a
    HARD block that override never bypasses."""
    return (
        rule.startswith("rest_")
        or rule.startswith("fdp_")
        or rule.startswith("hours_")
    )


def _assignment_score(readiness_score: float, monthly: float, max_monthly: float,
                      rested: bool, fdp_min, qualified: bool) -> int:
    """Smart weighted assignment score 0–100 (#3). Higher = better candidate.
    Weights: readiness 40% · fewest monthly hours 20% · rested 20% ·
    least projected FDP 10% · qualification fit 10%. Ranking aid only — it
    NEVER gates assignment (assign_crew remains the authority)."""
    f_ready = max(0.0, min(readiness_score / 100.0, 1.0))
    f_hours = 1.0 - min(monthly / max_monthly, 1.0) if max_monthly else 1.0
    f_rest  = 1.0 if rested else 0.3
    f_fdp   = (1.0 - min(float(fdp_min) / 780.0, 1.0)) if fdp_min else 1.0
    f_qual  = 1.0 if qualified else 0.4
    return round(100 * (0.40 * f_ready + 0.20 * f_hours + 0.20 * f_rest
                        + 0.10 * f_fdp + 0.10 * f_qual))


def _rank_candidates(cands: list) -> list:
    """Sort suggestion candidates: hard-BLOCKED always last, otherwise highest
    assignment_score first. Stamps a 1-based `assignment_rank`. Mutates + returns."""
    def _blocked(r):
        return r.get("compliance_status") == "BLOCKED" or r.get("readiness_status") == "BLOCKED"
    cands.sort(key=lambda r: (1 if _blocked(r) else 0, -r.get("assignment_score", 0)))
    for idx, r in enumerate(cands, start=1):
        r["assignment_rank"] = idx
    return cands


# Roster section for a crew RANK — one of the 6 GenDec sections. Delegates to
# the role registry (single source of truth: flight_deck / cabin_crew /
# technical_operations / ground_operations / flight_security / observer).
def _bucket_for_rank(rank: str | None) -> str:
    return role_bucket(rank)


@router.post("", status_code=201)
async def assign_crew(data: dict, current_user: CurrentUser, sb: SbClient):
    # Top-level role gate. Allocator-rank limits (below) still apply, but
    # without this gate anyone holding a token could create assignments.
    _ensure_assigner(current_user)
    flight_id = data["flight_id"]
    crew_id = data["crew_id"]
    is_override = data.get("is_override", False)
    override_reason = (data.get("override_reason") or "").strip()
    risk_level = (data.get("risk_level") or "medium").strip().lower()
    # Deadhead / positioning crew — crew RIDING the flight, not OPERATING it.
    # Validated against the same enum the DB CHECK constraint enforces, so an
    # unknown value fails fast at the API boundary (not at insert time).
    duty_type = (data.get("duty_type") or "operating").strip().lower()
    if duty_type not in _DUTY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"duty_type must be one of {sorted(_DUTY_TYPES)}",
        )
    is_operating = duty_type == "operating"
    # Overriding the compliance gate (FDP/rest/docs) is a SUPERVISOR-only
    # action and must be justified — otherwise any assigner could silently
    # bypass safety limits.
    if is_override:
        if current_user.get("role") not in ("super_admin", "admin", "ops_manager") \
                and not current_user.get("is_superuser"):
            raise ForbiddenError("التجاوز (Override) يتطلب صلاحية مشرف")
        if not override_reason:
            raise HTTPException(status_code=422, detail="سبب التجاوز مطلوب عند التجاوز")

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

    # Category is DERIVED from the crew rank — single source of truth. It picks
    # the flight-roster bucket (flight_deck / cabin_crew / ground_operations)
    # AND decides which compliance path runs below.
    category = category_for_rank(crew_rank)            # pilot|cabin|other (for capacity)
    assignment_bucket = _bucket_for_rank(crew_rank)    # one of the 6 roster sections
    # Operational-only roles (technical / ground / security / observer) link to
    # the flight but are NOT aircraft crew: light check, no complement count.
    is_operational = is_operational_only(crew_rank)
    crew_name = crew.get("full_name_ar") or crew.get("full_name_en") or crew_id

    # Specialty schedulers + broad allocators may only assign crew of their own
    # specialty (registry-driven; legacy + new ranks both resolve). General
    # scheduler/admins are unrestricted. Supervisors may override.
    if not is_override and not _role_may_assign_rank(user_role, crew_rank):
        raise ForbiddenError("هذا الدور يمكنه فقط تكليف طاقم اختصاصه")

    # Check duplicate
    dup = sb.table("assignments").select("id").eq("flight_id", flight_id).eq("crew_id", crew_id).execute()
    if dup.data:
        raise ConflictError(f"Crew member already assigned to flight {flight['flight_number']}")

    # Crew already on this flight — needed by BOTH the DNP gate (always) and
    # the complement-capacity gate (overridable). Fetched once.
    # `duty_type` is also pulled so the capacity gate can ignore deadhead /
    # standby / observer / training rows (they ride the flight but don't fill
    # an operating slot).
    existing_assignments = sb.table("assignments") \
        .select("crew_id, duty_type").eq("flight_id", flight_id).execute()
    assigned_crew_ids = [r["crew_id"] for r in (existing_assignments.data or []) if r.get("crew_id")]
    operating_crew_ids = [r["crew_id"] for r in (existing_assignments.data or [])
                          if r.get("crew_id") and (r.get("duty_type") or _OPERATING) == _OPERATING]

    # ── Do-Not-Pair (DNP) — ALWAYS enforced, even under override ──
    # A DNP decision is a safety/integrity ruling, never an administratively
    # overridable FTL limit.
    if assigned_crew_ids:
        dnp_pairs = get_approved_dnp_pairs(sb, current_user["company_id"])
        for (a, b) in dnp_pairs:
            for existing_id in assigned_crew_ids:
                if (crew_id == a and existing_id == b) or (crew_id == b and existing_id == a):
                    raise ForbiddenError(
                        "لا يمكن تكليف هذا العضو — يوجد قرار عدم تطيير (DNP) مع عضو مكلّف بنفس الرحلة"
                    )

    # ── Crew-complement capacity (over-staffing) — overridable ──
    # A flight takes ONLY its required complement per position. Supervisors may
    # bypass this with is_override (e.g. deliberate augmented crew).
    # Non-operating duty types (deadhead/standby/observer/training) are RIDING
    # the flight, not filling an operating slot — the cap doesn't apply to
    # them and they don't count toward existing in-category/in-role totals.
    if not is_override and is_operating:
        if category in ("pilot", "cabin", "engineer"):
            dur_h = flight.get("duration_hours")
            if dur_h is None:
                _dep = datetime.fromisoformat(flight["departure_time"].replace("Z", "+00:00")) \
                    if flight.get("departure_time") else None
                _arr = datetime.fromisoformat(flight["arrival_time"].replace("Z", "+00:00")) \
                    if flight.get("arrival_time") else None
                dur_h = (_arr - _dep).total_seconds() / 3600 if (_dep and _arr) else None
            cap = required_for_category(flight.get("aircraft_type"), category, dur_h)
            if cap is not None and operating_crew_ids:
                rank_rows = sb.table("crew").select("rank") \
                    .in_("id", operating_crew_ids).execute().data or []
                in_cat = sum(1 for r in rank_rows
                             if category_for_rank(r.get("rank")) == category)
                if in_cat >= cap:
                    _label = {"pilot": "الطيارين", "cabin": "طاقم الضيافة",
                              "engineer": "المهندسين"}[category]
                    raise ForbiddenError(
                        f"اكتمل عدد {_label} لهذه الرحلة ({in_cat}/{cap}) — "
                        f"لا يمكن تعيين أكثر من العدد المطلوب"
                    )

        # Per-role cap for OPERATIONAL roles (AME / L/SH / IFSO / OBS / US / Tech).
        # Same rule: only operating rows count toward the cap.
        if is_operational:
            op_template = operational_expected_by_role(flight.get("aircraft_type"))
            canonical_role = normalize_role(crew_rank)
            per_role_cap = op_template.get(canonical_role, 0)
            if per_role_cap > 0 and operating_crew_ids:
                rank_rows = sb.table("crew").select("rank") \
                    .in_("id", operating_crew_ids).execute().data or []
                in_role = sum(1 for r in rank_rows
                              if normalize_role(r.get("rank")) == canonical_role)
                if in_role >= per_role_cap:
                    _role_lbl = role_label(crew_rank, arabic=True)
                    raise ForbiddenError(
                        f"اكتمل عدد {_role_lbl} لهذه الرحلة "
                        f"({in_role}/{per_role_cap}) — "
                        f"اختر رحلة أخرى أو استخدم تجاوز المشرف"
                    )

    # ── Compliance — path depends on role + duty_type ───────────
    # Non-operating crew (deadhead / standby / observer / training) RIDE the
    # flight as passengers, not as duty time — they get the same LIGHT check
    # as operational roles (active + has account + not blocked), and skip
    # the full FTL/FDP/rest/qualification compliance entirely.
    if is_operational or not is_operating:
        _status = (crew.get("status") or "active").lower()
        if _status in ("blocked", "suspended", "inactive", "terminated"):
            raise CrewBlockedError(crew_name, "الموظف غير نشط أو محظور")
        _acct = sb.table("users").select("is_active").eq("crew_id", crew_id).execute().data or []
        if _acct and _acct[0].get("is_active") is False:
            raise CrewBlockedError(crew_name, "حساب المستخدم غير مفعّل")
    else:
        # Aircraft crew (flight deck / cabin): FULL compliance ALWAYS runs.
        # Override bypasses ONLY administratively-overridable FTL/FDP/rest.
        # HARD blocks (time conflict / double booking, crew blocked-suspended,
        # missing type rating, expired docs/training, engine self-error) are
        # enforced even under override.
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
            flight_aircraft_type=flight.get("aircraft_type"),
        )

        blocking = [i for i in compliance.get("issues", []) if i.get("is_blocking")]
        hard     = [i for i in blocking if not _is_overridable_block(i.get("rule", ""))]
        ftl      = [i for i in blocking if _is_overridable_block(i.get("rule", ""))]

        # Hard blocks: never bypassed, even with a valid override.
        if hard:
            reasons = "; ".join(i.get("message_ar", "") for i in hard) or "مخالفة أمان صارمة"
            raise CrewBlockedError(crew_name, reasons)
        # FTL/FDP/rest: bypassed only by an authorised, justified override.
        if ftl and not is_override:
            reasons = "; ".join(i.get("message_ar", "") for i in ftl) or "تجاوز حدود FTL/FDP"
            raise CrewBlockedError(crew_name, reasons)

    # NOTE: assignments table has no `company_id` column — isolation is
    # enforced via the flight relationship instead (see get_assignments).
    assignment = {
        "id": str(uuid.uuid4()),
        "flight_id": flight_id,
        "crew_id": crew_id,
        "assigned_by": current_user["id"],
        "assignment_type": assignment_bucket,
        "assigned_role": crew_rank,
        # Snapshot the crew member's airline AT assignment time, so historical
        # reports stay correct if the crew later moves to another company.
        "operator_company_id": crew.get("operator_company_id"),
        "duty_type": duty_type,         # operating | deadhead | standby | observer | training
        "is_override": is_override,
        "override_reason": override_reason if is_override else None,
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

    # ── Audit trail — every assignment + every override is recorded ──
    try:
        import json as _json
        sb.table("audit_log").insert({
            "user_id":         current_user["id"],
            "user_name":       current_user.get("name_ar") or current_user.get("name_en") or current_user.get("email", ""),
            "action":          "override_assignment" if is_override else "assign_crew",
            "entity_type":     "assignment",
            "entity_id":       saved.get("id"),
            "is_override":     bool(is_override),
            "override_reason": override_reason or None,
            "after_data":      _json.dumps({
                "flight_id":       flight_id,
                "flight_number":   flight.get("flight_number"),
                "crew_id":         crew_id,
                "crew_name":       crew.get("full_name_ar") or crew.get("full_name_en"),
                "risk_level":      risk_level if is_override else None,
                "assignment_type": assignment.get("assignment_type"),
            }, ensure_ascii=False),
            "company_id":      current_user["company_id"],
            "created_at":      datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        logger.exception("audit_log write failed for assignment %s", saved.get("id"))

    # ── Crew notification — ONLY when the flight is already published ──────────
    # Per ops: crew are notified when the roster is PUBLISHED, not on every draft
    # edit. So assigning to a DRAFT flight stays silent; assigning to a flight
    # that's already live notifies that crew member immediately. Publishing a
    # flight fans the notification out to everyone then (flights.publish_flight).
    # Best-effort: a notify failure never fails the assign.
    if flight.get("publish_status") == "published":
        try:
            _notify_crew_assigned(sb, crew_id, flight)
        except Exception:
            logger.exception("assign_crew notify failed crew=%s flight=%s", crew_id, flight_id)

    return saved


@router.post("/connected-duty", status_code=201)
async def assign_connected_duty(data: dict, current_user: CurrentUser, sb: SbClient):
    """Assign one or more crew to a set of flights flown as a SINGLE connected
    duty (same-day rotation / turnaround chain).

    Body:
      flight_ids:        [str]   the duty's sectors
      crew_member_ids:   [str]   crew to put on the whole duty
      duty_type:         str     'connected' (informational)
      notes:             str?
      is_override:       bool    supervisor override of a BLOCKING result
      override_reason:   str     required when is_override
      preview:           bool    when true → compliance only, NO writes

    The compliance engine treats the gaps between the duty's sectors as
    turnarounds (not rest) and bounds the whole duty by one FDP limit.
    """
    _ensure_assigner(current_user)
    flight_ids = [str(x) for x in (data.get("flight_ids") or [])]
    crew_ids   = [str(x) for x in (data.get("crew_member_ids") or [])]
    preview    = bool(data.get("preview", False))
    is_override = bool(data.get("is_override", False))
    override_reason = (data.get("override_reason") or "").strip()

    if len(flight_ids) < 2:
        raise HTTPException(status_code=422, detail="flight_ids must contain at least two flights")
    if not crew_ids:
        raise HTTPException(status_code=422, detail="crew_member_ids is required")

    if is_override:
        if current_user.get("role") not in ("super_admin", "admin", "ops_manager") \
                and not current_user.get("is_superuser"):
            raise ForbiddenError("التجاوز (Override) يتطلب صلاحية مشرف")
        if not override_reason:
            raise HTTPException(status_code=422, detail="سبب التجاوز مطلوب عند التجاوز")

    # Validate every flight belongs to the caller's company.
    flights = sb.table("flights").select(
        "id,aircraft_type,departure_time,arrival_time,duration_hours"
    ).in_("id", flight_ids).eq("company_id", current_user["company_id"]).execute().data or []
    found = {f["id"] for f in flights}
    missing = [fid for fid in flight_ids if fid not in found]
    if missing:
        raise NotFoundError("Flight", ", ".join(missing))

    # ── Complement capacity ───────────────────────────────────────
    # Each sector takes only its required complement per position. Reject if
    # adding this crew would exceed a category's cap on any flight (override skips).
    if not is_override:
        _new_ranks = sb.table("crew").select("id,rank").in_("id", crew_ids).execute().data or []
        _new_cats = [category_for_rank(r.get("rank")) for r in _new_ranks]
        fmap = {f["id"]: f for f in flights}
        # Batch (was N+1): all existing assignments for ALL sectors in ONE query,
        # then all already-assigned crew ranks in ONE query — grouped in Python.
        _exist_rows = sb.table("assignments").select("flight_id,crew_id") \
            .in_("flight_id", flight_ids).execute().data or []
        _exist_by_flight: dict = {}
        _exist_crew_ids: set = set()
        for r in _exist_rows:
            _exist_by_flight.setdefault(r["flight_id"], []).append(r.get("crew_id"))
            if r.get("crew_id"):
                _exist_crew_ids.add(r["crew_id"])
        _rank_by_crew: dict = {}
        if _exist_crew_ids:
            for r in (sb.table("crew").select("id,rank")
                      .in_("id", list(_exist_crew_ids)).execute().data or []):
                _rank_by_crew[r["id"]] = r.get("rank")
        for fid in flight_ids:
            fl = fmap.get(fid, {})
            dur_h = fl.get("duration_hours")
            if dur_h is None and fl.get("departure_time") and fl.get("arrival_time"):
                _d = datetime.fromisoformat(fl["departure_time"].replace("Z", "+00:00"))
                _a = datetime.fromisoformat(fl["arrival_time"].replace("Z", "+00:00"))
                dur_h = (_a - _d).total_seconds() / 3600
            exist_ranks = [{"rank": _rank_by_crew.get(eid)} for eid in _exist_by_flight.get(fid, [])]
            for cat in ("pilot", "cabin", "engineer"):
                cap = required_for_category(fl.get("aircraft_type"), cat, dur_h)
                if cap is None:
                    continue
                cur = sum(1 for r in (exist_ranks or []) if category_for_rank(r.get("rank")) == cat)
                add = sum(1 for c in _new_cats if c == cat)
                if cur + add > cap:
                    _label = {"pilot": "الطيارين", "cabin": "طاقم الضيافة",
                              "engineer": "المهندسين"}[cat]
                    raise ForbiddenError(
                        f"اكتمل عدد {_label} لإحدى رحلات الواجب ({cur + add}/{cap}) — "
                        f"لا يمكن تعيين أكثر من العدد المطلوب"
                    )

    engine = ComplianceEngine(sb)
    previews = [engine.check_connected_duty(cid, flight_ids) for cid in crew_ids]

    # Preview mode → compliance only, no writes.
    if preview:
        return {"preview": True, "duty_type": data.get("duty_type", "connected"),
                "crews": previews}

    # Split each crew's blocking issues like the single-assign path: HARD blocks
    # (duty overlap, not-contiguous, crew blocked, type rating, expired docs/
    # training, engine error) are enforced even under override; only FTL/FDP/rest
    # limits may be bypassed by an authorised, justified override.
    for p in previews:
        b = [i for i in p.get("issues", []) if i.get("is_blocking")]
        hard = [i for i in b if not _is_overridable_block(i.get("rule", ""))]
        if hard:
            raise CrewBlockedError(
                p.get("crew_name_ar") or p.get("crew_id", ""),
                "; ".join(i.get("message_ar", "") for i in hard) or "مخالفة أمان صارمة")
    if not is_override:
        for p in previews:
            ftl = [i for i in p.get("issues", [])
                   if i.get("is_blocking") and _is_overridable_block(i.get("rule", ""))]
            if ftl:
                raise CrewBlockedError(
                    p.get("crew_name_ar") or p.get("crew_id", ""),
                    "; ".join(i.get("message_ar", "") for i in ftl) or "تجاوز حدود FTL/FDP")

    # Roster bucket + role per crew, derived from rank (single source of truth)
    # — same scheme as the single-assign path so connected duties group into the
    # 3 flight-roster sections too.
    _rank_rows = sb.table("crew").select("id,rank,operator_company_id").in_("id", crew_ids).execute().data or []
    _bucket_by_crew = {r["id"]: _bucket_for_rank(r.get("rank")) for r in _rank_rows}
    _role_by_crew = {r["id"]: r.get("rank") for r in _rank_rows}
    _op_co_by_crew = {r["id"]: r.get("operator_company_id") for r in _rank_rows}

    # Atomic-ish insert: track ids, roll back on any failure.
    now = datetime.now(timezone.utc).isoformat()
    inserted_ids: list[str] = []
    # Batch (was N+1: one SELECT per crew×flight): fetch all existing pairs once.
    _existing_pairs: set = set()
    for r in (sb.table("assignments").select("flight_id,crew_id")
              .in_("flight_id", flight_ids).in_("crew_id", crew_ids).execute().data or []):
        _existing_pairs.add((r.get("flight_id"), r.get("crew_id")))
    try:
        for cid in crew_ids:
            for fid in flight_ids:
                if (fid, cid) in _existing_pairs:
                    continue  # already assigned — idempotent
                aid = str(uuid.uuid4())
                sb.table("assignments").insert({
                    "id": aid, "flight_id": fid, "crew_id": cid,
                    "assigned_by": current_user["id"],
                    "assignment_type": _bucket_by_crew.get(cid, "ground_operations"),
                    "assigned_role": _role_by_crew.get(cid),
                    "operator_company_id": _op_co_by_crew.get(cid),
                    "is_override": is_override,
                    "override_reason": override_reason or None,
                    "acknowledged": False,
                    "created_at": now, "updated_at": now,
                }).execute()
                inserted_ids.append(aid)
    except Exception as e:
        for aid in inserted_ids:
            try:
                sb.table("assignments").delete().eq("id", aid).execute()
            except Exception:
                logger.exception("rollback failed for assignment %s", aid)
        logger.exception("connected-duty assign failed")
        raise HTTPException(status_code=502, detail=f"assignment failed, rolled back: {str(e)[:200]}")

    # Audit the duty action.
    try:
        sb.table("audit_logs").insert({
            "id": str(uuid.uuid4()),
            "user_id": current_user["id"],
            "company_id": current_user["company_id"],
            "action": "assign_connected_duty",
            "is_override": is_override,
            "override_reason": override_reason or None,
            "details": {"flight_ids": flight_ids, "crew_member_ids": crew_ids,
                        "notes": data.get("notes")},
            "created_at": now,
        }).execute()
    except Exception:
        logger.warning("connected-duty audit log skipped")

    return {"assigned": len(inserted_ids), "crews": len(crew_ids),
            "flights": len(flight_ids), "is_override": is_override,
            "previews": previews}


@router.get("/suggest/{flight_id}")
async def suggest_crew(
    flight_id: str, current_user: CurrentUser, sb: SbClient,
    limit: int = Query(12, ge=1, le=30),
):
    """Rank crew for a flight: qualified + compliant + fewest hours first.

    Cheap pre-filter on batch data (status, time-conflict, type-rating, monthly
    hours), then the AUTHORITATIVE compliance engine runs only on the shortlist
    — so the result matches what the assign endpoint would enforce, without
    N×deep-checks across the whole roster."""
    _ensure_assigner(current_user)
    cid = current_user["company_id"]

    fl = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", cid).execute()
    if not fl.data:
        raise NotFoundError("Flight", flight_id)
    flight = fl.data[0]

    def _dt(s):
        # Always return UTC-AWARE (assume UTC for naive values). A stored time
        # without a tz would otherwise be naive and break the `a2 <= now`
        # comparison below (naive vs aware → TypeError → 500).
        try:
            d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    dep = _dt(flight.get("departure_time"))
    arr = _dt(flight.get("arrival_time"))
    intl = (flight.get("origin_code", "").upper() not in IRAQI_AIRPORTS or
            flight.get("destination_code", "").upper() not in IRAQI_AIRPORTS)
    ac_type = flight.get("aircraft_type")

    # Pull ONLY the columns the cheap pre-filter + output need (not select("*")) —
    # keeps the full-roster scan light even with thousands of rows. The
    # authoritative engine re-loads its own data for the shortlist by crew_id.
    # NOTE: the crew table has `base` and `aircraft_qualifications` only — there
    # is NO `base_code` and NO `aircraft_type` column (aircraft_type lives on
    # flights). Requesting a non-existent column makes PostgREST reject the whole
    # SELECT (APIError → 500). Select only columns that exist; the qualification
    # check below falls back to aircraft_qualifications when aircraft_type is absent.
    crew_rows = sb.table("crew").select(
        "id,status,rank,aircraft_qualifications,"
        "employee_id,full_name_ar,full_name_en,base"
    ).eq("company_id", cid).execute().data or []
    # A specialty scheduler only ever sees/suggests their own ranks.
    restricted = _restricted_ranks(current_user)
    if restricted is not None:
        crew_rows = [c for c in crew_rows if c.get("rank") in restricted]
    crew_ids = [c["id"] for c in crew_rows if c.get("id")]

    # Batch-load assignments for THIS company's crew + their flights once
    # (scoped — avoids fetching every company's assignments and per-crew queries).
    asgs = []
    if crew_ids:
        asgs = sb.table("assignments").select("crew_id,flight_id") \
            .in_("crew_id", crew_ids).execute().data or []
    fids = list({a["flight_id"] for a in asgs if a.get("flight_id")})
    fmap = {}
    if fids:
        frows = sb.table("flights").select(
            "id,departure_time,arrival_time,duration_hours,status"
        ).in_("id", fids).execute().data or []
        fmap = {f["id"]: f for f in frows}
    by_crew: dict[str, list] = {}
    for a in asgs:
        f = fmap.get(a.get("flight_id"))
        if f:
            by_crew.setdefault(a["crew_id"], []).append(f)

    month_start = dep.date().replace(day=1) if dep else None
    norm = ComplianceEngine._norm_aircraft_types
    flight_types = norm(ac_type)

    now = datetime.now(timezone.utc)
    cands = []
    for c in crew_rows:
        status = (c.get("status") or "active")
        if status in ("blocked", "suspended"):
            continue
        conflict = False
        monthly = 0.0
        last_arr = None  # most recent PAST arrival → rest basis
        for f in by_crew.get(c["id"], []):
            if f.get("status") == "cancelled":
                continue
            d = _dt(f.get("departure_time"))
            a2 = _dt(f.get("arrival_time"))
            if dep and arr and d and a2 and d < arr and dep < a2:
                conflict = True
            if d and month_start and d.date() >= month_start:
                monthly += float(f.get("duration_hours") or 0)
            if a2 and a2 <= now and (last_arr is None or a2 > last_arr):
                last_arr = a2
        if conflict:
            continue
        qualified = True
        if flight_types:
            cset = norm(c.get("aircraft_qualifications")) | norm(c.get("aircraft_type"))
            if cset and not (flight_types & cset):
                qualified = False
        rested = last_arr is None or \
            (now - last_arr).total_seconds() / 3600.0 >= settings.MIN_REST_HOURS
        cands.append({"crew": c, "monthly": monthly, "qualified": qualified,
                      "on_leave": status == "on_leave", "rested": rested})

    # Cheap rank → shortlist → authoritative compliance on the shortlist only.
    cands.sort(key=lambda x: (0 if x["qualified"] else 1,
                              0 if not x["on_leave"] else 1, x["monthly"]))
    engine = ComplianceEngine(sb)
    out = []
    for x in cands[:limit]:
        c = x["crew"]
        res = engine.check_crew(
            crew_id=c["id"], flight_id=flight_id,
            flight_departure=dep, flight_arrival=arr,
            is_international=intl, flight_aircraft_type=ac_type,
        )
        readiness = engine._readiness_from_result(res)

        # ── Smart weighted assignment score (#3) ──────────────────
        max_monthly = float(c.get("max_monthly_hours") or settings.MAX_MONTHLY_HOURS)
        fdp_min = None
        for i in res.get("issues", []):
            if i.get("rule", "").startswith("fdp_"):
                fdp_min = (i.get("detail") or {}).get("fdp_minutes") \
                          or (i.get("detail") or {}).get("actual_minutes")
                break
        score = _assignment_score(readiness["readiness_score"], x["monthly"],
                                  max_monthly, x["rested"], fdp_min, x["qualified"])
        reasons = [
            f"الجاهزية {readiness['readiness_score']}",
            f"ساعات {x['monthly']:.0f}h",
            "راحة مكتملة" if x["rested"] else "قيد الراحة",
            "مؤهل" if x["qualified"] else "غير مؤهل",
        ]

        out.append({
            "crew_id":           c["id"],
            "employee_id":       c.get("employee_id", ""),
            "name_ar":           c.get("full_name_ar", ""),
            "name_en":           c.get("full_name_en", ""),
            "rank":              c.get("rank", ""),
            "base":              c.get("base", ""),
            "monthly_hours":     round(x["monthly"], 1),
            "qualified":         x["qualified"],
            "compliance_status": res.get("status"),
            "blocking_reasons":  res.get("blocking_reasons", []),
            # Advisory readiness (Phase A) — does NOT gate assignment.
            "readiness_score":   readiness["readiness_score"],
            "readiness_status":  readiness["readiness_status"],
            "readiness_color":   readiness["readiness_color"],
            "readiness_reasons": readiness["readiness_reasons"],
            # Smart weighted assignment (Phase #3) — ranking aid, NOT a gate.
            "assignment_score":  score,
            "assignment_reasons": reasons,
        })

    # Hard-blocked candidates always sink to the bottom; otherwise highest
    # weighted score first. Candidates are RANKED, never removed (the only
    # removals were hard time-conflict / inactive, filtered above).
    _rank_candidates(out)

    return {"flight_id": flight_id, "flight_number": flight.get("flight_number"),
            "aircraft_type": ac_type, "candidates": out}


@router.get("/crew-readiness")
async def crew_readiness_board(current_user: CurrentUser, sb: SbClient):
    """BATCHED advisory readiness for the whole roster (for the scheduling board's
    per-crew status badge). A handful of bulk queries, never N×. ADVISORY ONLY —
    does not gate assignment. Returns {crew: {crew_id: {...}}}."""
    _ensure_assigner(current_user)
    engine = ComplianceEngine(sb)
    board = engine.batch_readiness(current_user["company_id"])
    return {"crew": board, "count": len(board)}


@router.get("/projection/{flight_id}/{crew_id}")
async def assignment_projection(flight_id: str, crew_id: str,
                                current_user: CurrentUser, sb: SbClient):
    """LIVE legal projection for putting `crew_id` on `flight_id` — shown BEFORE
    assigning. Returns projected hours (current + this flight), FDP, rest, the
    readiness score/status and an expected decision. ADVISORY ONLY — the binding
    decision is still made by assign_crew."""
    _ensure_assigner(current_user)
    cid = current_user["company_id"]

    fl = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", cid).execute()
    if not fl.data:
        raise NotFoundError("Flight", flight_id)
    flight = fl.data[0]
    cr = sb.table("crew").select("id,status,rank,max_monthly_hours") \
        .eq("id", crew_id).eq("company_id", cid).execute()
    if not cr.data:
        raise NotFoundError("Crew member", crew_id)
    crew = cr.data[0]

    def _dt(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
        except Exception:
            return None
    dep = _dt(flight.get("departure_time"))
    arr = _dt(flight.get("arrival_time"))
    intl = (flight.get("origin_code", "").upper() not in IRAQI_AIRPORTS or
            flight.get("destination_code", "").upper() not in IRAQI_AIRPORTS)
    ac_type = flight.get("aircraft_type")

    engine = ComplianceEngine(sb)
    # Current cumulative hours for THIS crew only (one bulk pass).
    cur = engine.batch_readiness(cid, crew_rows=[crew]).get(crew_id, {})
    new_dur = float(flight.get("duration_hours") or 0)
    if not new_dur and dep and arr:
        new_dur = max(0.0, (arr - dep).total_seconds() / 3600.0)

    # Full per-flight compliance (already PROJECTS the new flight into FTL — fix #1).
    res = engine.check_crew(crew_id=crew_id, flight_id=flight_id,
                            flight_departure=dep, flight_arrival=arr,
                            is_international=intl, flight_aircraft_type=ac_type)
    readiness = engine._readiness_from_result(res)
    status = res.get("status")
    decision = ("BLOCKED" if status == "BLOCKED"
                else "WARNING" if status in ("RED", "YELLOW") else "READY")
    # FDP minutes if the engine surfaced an FDP issue with detail.
    fdp_minutes = None
    for i in res.get("issues", []):
        if i.get("rule", "").startswith("fdp_"):
            fdp_minutes = (i.get("detail") or {}).get("fdp_minutes") \
                          or (i.get("detail") or {}).get("actual_minutes")
            break

    return {
        "flight_id": flight_id, "crew_id": crew_id,
        "flight_number": flight.get("flight_number"),
        "new_flight_hours": round(new_dur, 1),
        "projected": {
            "monthly_hours": round(cur.get("monthly_flight_hours", 0) + new_dur, 1),
            "last_28day_hours": round(cur.get("last_28day_hours", 0) + new_dur, 1),
            "yearly_hours": round(cur.get("yearly_hours", 0) + new_dur, 1),
            "max_monthly_hours": cur.get("max_monthly_hours", settings.MAX_MONTHLY_HOURS),
        },
        "fdp_minutes": fdp_minutes,
        "rest_status": cur.get("rest_status"),
        "next_available_at": cur.get("next_available_at"),
        "compliance_status": status,
        "decision": decision,
        **readiness,
    }


@router.delete("/{assignment_id}")
async def remove_assignment(assignment_id: str, current_user: CurrentUser, sb: SbClient):
    # Removing an assignment is a scheduling action — same gate as creating
    # one. Crew should use /decline if they cannot fly; deletion erases the
    # audit trail and must stay with ops.
    _ensure_assigner(current_user)
    existing = sb.table("assignments").select("id,flight_id,crew_id").eq("id", assignment_id).execute()
    if not existing.data:
        raise NotFoundError("Assignment", assignment_id)

    # Verify the assignment's flight belongs to this company
    flight_id = existing.data[0].get("flight_id")
    crew_id   = existing.data[0].get("crew_id")
    flight = None
    if flight_id:
        flight_check = sb.table("flights").select("*").eq("id", flight_id).eq("company_id", current_user["company_id"]).execute()
        if not flight_check.data:
            raise NotFoundError("Assignment", assignment_id)
        flight = flight_check.data[0]

    sb.table("assignments").delete().eq("id", assignment_id).execute()

    # ── Notify the crew member that they were removed from the flight ──
    # Assigning fans out a notification; removal must too, otherwise crew keep
    # a stale duty in their roster/app.
    try:
        if crew_id and flight:
            crew_user = sb.table("users").select("id").eq("crew_id", crew_id).execute()
            if crew_user.data:
                uid = crew_user.data[0]["id"]
                fnum = flight.get("flight_number", "")
                origin = flight.get("origin_code", "")
                dest   = flight.get("destination_code", "")
                title_ar = f"أُلغي تكليفك برحلة {fnum}"
                title_en = f"You're removed from flight {fnum}"
                msg_ar = f"رحلة {fnum} ({origin} → {dest}) — لم تعد مكلّفاً بها."
                msg_en = f"Flight {fnum} ({origin} → {dest}) — you are no longer assigned."
                sb.table("notifications").insert({
                    "id":                str(uuid.uuid4()),
                    "user_id":           uid,
                    "target_user_id":    uid,
                    "company_id":        current_user["company_id"],
                    "type":              "crew_unassigned",
                    "title_ar":          title_ar,
                    "title_en":          title_en,
                    "message_ar":        msg_ar,
                    "message_en":        msg_en,
                    "body_ar":           msg_ar,
                    "body_en":           msg_en,
                    "reference_id":      flight_id,
                    "reference_type":    "flight",
                    "related_flight_id": flight_id,
                    "related_crew_id":   crew_id,
                    "is_read":           False,
                    "created_at":        datetime.now(timezone.utc).isoformat(),
                    "updated_at":        datetime.now(timezone.utc).isoformat(),
                }).execute()
                try:
                    push_service.send_to_users(sb, [uid], title=title_ar,
                        body=f"{fnum} ({origin} → {dest})",
                        data={"type": "crew_unassigned", "reference_id": str(flight_id),
                              "reference_type": "flight"})
                except Exception as pe:
                    logger.warning("Push (unassign) failed for crew %s: %s", crew_id, pe)
    except Exception as e:
        logger.warning("Unassign notification failed for crew %s: %s", crew_id, e)

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
    result = sb.table("assignments").select("*, crew(full_name_ar, full_name_en, rank, employee_id)").eq("flight_id", flight_id).limit(500).execute()
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
