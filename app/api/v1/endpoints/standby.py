"""Standby / Reserve crew management.

Schedulers put crew on call (Airport / Home / Ready / Long-call) for a window;
when a flight is short-crewed, Operations can call them out. The suggest
endpoint ranks eligible standby crew for a flight using the ComplianceEngine
(so a blocked/over-FDP reserve is never offered first).
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from app.api.deps import CurrentUser, SbClient
from app.core.audit import write_audit
from app.core.config import settings
from app.core.exceptions import NotFoundError, ForbiddenError, ConflictError
from app.core.compliance_engine import ComplianceEngine, IRAQI_AIRPORTS
from app.services import push_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/standby", tags=["Standby"])

# Same population that may assign crew may manage standby.
_MANAGERS = {
    "super_admin", "admin", "ops_manager", "scheduler", "scheduler_admin",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "flight_movement", "flight_movement_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}

_VALID_TYPES = {"AIRPORT_STANDBY", "HOME_STANDBY", "READY_RESERVE", "LONG_CALL"}
_VALID_STATUS = {"ACTIVE", "CALLED_OUT", "ASSIGNED", "EXPIRED", "CANCELLED"}


def _ensure_manager(user: dict) -> None:
    if user.get("role") not in _MANAGERS and not user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بإدارة الاحتياط")


# Maintenance actions (expiry sweep) are supervisory — same population that runs
# the acceptance-reminder sweep.
_SUPERVISORS = {"super_admin", "admin", "ops_manager", "scheduler_admin"}


def _ensure_supervisor(user: dict) -> None:
    if user.get("role") not in _SUPERVISORS and not user.get("is_superuser"):
        raise ForbiddenError("الإدمن / مدير العمليات / مشرف الجدولة فقط")


def _parse_dt(value):
    """Parse an ISO timestamp; None on anything unparseable."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _notify_reserve_callout(sb, company_id: str, before: dict,
                            flight_id: Optional[str]) -> dict:
    """R1 — tell the reserve crew member they've been called out: an in-app
    notification + a best-effort push.

    FAIL-SOFT by construction: this never raises into callout, push delivery is
    delegated to push_service (which itself never raises and tolerates a missing
    device token), and the in-app row is written BEFORE the push so a push
    failure can't lose the in-app message. Company-scoped: the recipient user is
    resolved within `company_id`. R1 adds NO accept/reject, NO assignment
    bridge, NO escalation — purely an alert."""
    try:
        crew_id = before.get("crew_id")
        if not crew_id:
            return {"notified": False, "reason": "no_crew"}
        urs = (sb.table("users").select("id,crew_id")
               .eq("company_id", company_id).eq("is_active", True)
               .eq("crew_id", crew_id).execute().data) or []
        uid = urs[0]["id"] if urs else None
        if not uid:                       # crew member has no login account
            return {"notified": False, "reason": "no_user"}

        flight_num = None
        if flight_id:
            fr = (sb.table("flights").select("flight_number")
                  .eq("id", flight_id).eq("company_id", company_id)
                  .execute().data) or []
            if fr:
                flight_num = fr[0].get("flight_number")

        airport = before.get("airport_code")
        start, end = before.get("start_time"), before.get("end_time")
        bits_ar = ["تم استدعاؤك كاحتياط."]
        bits_en = ["You have been called out as reserve."]
        if flight_num:
            bits_ar.append(f"الرحلة: {flight_num}.")
            bits_en.append(f"Flight: {flight_num}.")
        if airport:
            bits_ar.append(f"المطار/القاعدة: {airport}.")
            bits_en.append(f"Airport/base: {airport}.")
        if start and end:
            bits_ar.append(f"الاحتياط من {start} إلى {end}.")
            bits_en.append(f"Standby {start} → {end}.")
        msg_ar, msg_en = " ".join(bits_ar), " ".join(bits_en)

        sb.table("notifications").insert({
            "id": str(uuid.uuid4()),
            "user_id": uid,
            "type": "standby_callout",
            "title_ar": "استدعاء احتياط",
            "title_en": "Reserve call-out",
            "message_ar": msg_ar,
            "message_en": msg_en,
            "reference_id": before.get("id"),
            "reference_type": "standby",
            "is_read": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

        push_service.send_to_users(
            sb, [uid], title="استدعاء احتياط", body=msg_ar,
            data={"type": "standby_callout", "reference_type": "standby",
                  "reference_id": before.get("id")})
        return {"notified": True, "user_id": uid}
    except Exception as e:
        logger.warning("standby callout notify failed for %s: %s",
                       before.get("id"), e)
        return {"notified": False, "reason": "error"}


def _notify_schedulers_standby(sb, company_id: str, row: dict,
                               kind: str, detail: Optional[str] = None) -> None:
    """R2 — tell the schedulers/ops how a reserve responded (accepted+assigned /
    rejected / accepted-but-assignment-failed). Fail-soft; NO escalation."""
    try:
        from app.api.v1.endpoints.flights import _SCHEDULER_NOTIFY_ROLES
        urs = (sb.table("users").select("id,role")
               .eq("company_id", company_id).eq("is_active", True)
               .execute().data) or []
        recipients = [u["id"] for u in urs
                      if u.get("role") in _SCHEDULER_NOTIFY_ROLES]
        if not recipients:
            return
        msgs = {
            "rejected": (f"رفض الطاقم استدعاء الاحتياط. السبب: {detail or '—'}",
                         f"Reserve callout rejected. Reason: {detail or '—'}"),
            "accepted_assigned": ("قبل الطاقم استدعاء الاحتياط وتم التعيين.",
                                  "Reserve accepted the callout and was assigned."),
            "accept_failed": (f"قبل الطاقم الاحتياط لكن فشل التعيين: {detail or '—'}",
                              f"Reserve accepted but assignment failed: {detail or '—'}"),
        }
        ar, en = msgs.get(kind, ("تحديث استدعاء احتياط", "Reserve callout update"))
        now = datetime.now(timezone.utc).isoformat()
        notifs = [{
            "id": str(uuid.uuid4()),
            "user_id": uid,
            "type": "standby_response",
            "title_ar": "ردّ على استدعاء احتياط",
            "title_en": "Reserve callout response",
            "message_ar": ar,
            "message_en": en,
            "reference_id": row.get("id"),
            "reference_type": "standby",
            "is_read": False,
            "created_at": now,
        } for uid in recipients]
        sb.table("notifications").insert(notifs).execute()
        push_service.send_to_users(
            sb, recipients, title="ردّ على استدعاء احتياط", body=ar,
            data={"type": "standby_response", "reference_type": "standby",
                  "reference_id": row.get("id")})
    except Exception as e:
        logger.warning("standby scheduler-notify failed for %s: %s",
                       row.get("id"), e)


def _resolve_assigner(sb, company_id: str, user_id: Optional[str]):
    """The responsible assigner for an accepted callout = the user who OWNS the
    standby (its `created_by`). Returns a current_user-shaped dict only if that
    user exists in this company AND holds an assigner role — otherwise None, so
    the caller surfaces a clear failure instead of silently elevating anyone."""
    if not user_id:
        return None
    from app.api.v1.endpoints.assignments import _ASSIGNERS
    urs = (sb.table("users")
           .select("id,role,name_ar,name_en,email,crew_id,crew_department,is_superuser")
           .eq("id", user_id).eq("company_id", company_id).execute().data) or []
    if not urs:
        return None
    u = urs[0]
    if u.get("role") not in _ASSIGNERS and not u.get("is_superuser"):
        return None
    return {
        "id": u["id"], "role": u.get("role"), "company_id": company_id,
        "name_ar": u.get("name_ar"), "name_en": u.get("name_en"),
        "email": u.get("email"), "crew_department": u.get("crew_department"),
        "is_superuser": bool(u.get("is_superuser")),
    }


def _enrich(sb, company_id: str, rows: list) -> list:
    """Attach crew name/rank to each standby row for display."""
    crew_ids = list({r["crew_id"] for r in rows if r.get("crew_id")})
    names: dict[str, dict] = {}
    if crew_ids:
        cres = sb.table("crew").select("id,full_name_ar,full_name_en,rank,base,roster_name") \
            .in_("id", crew_ids).execute().data or []
        names = {c["id"]: c for c in cres}
    for r in rows:
        c = names.get(r.get("crew_id"), {})
        r["crew_name_ar"] = c.get("full_name_ar", "")
        r["crew_name_en"] = c.get("full_name_en", "")
        r["crew_rank"]    = c.get("rank", "")
        r["crew_base"]    = c.get("base", "")
        r["roster_name"]  = c.get("roster_name")
    return rows


@router.get("")
async def list_standby(
    current_user: CurrentUser,
    sb: SbClient,
    status: Optional[str] = None,
):
    _ensure_manager(current_user)
    q = sb.table("standby_assignments").select("*").eq("company_id", current_user["company_id"])
    if status:
        q = q.eq("status", status.upper())
    rows = q.order("start_time", desc=False).execute().data or []
    return _enrich(sb, current_user["company_id"], rows)


@router.post("", status_code=201)
async def create_standby(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_manager(current_user)
    crew_id = (data.get("crew_id") or "").strip()
    if not crew_id:
        raise HTTPException(status_code=422, detail="crew_id مطلوب")
    if not data.get("start_time") or not data.get("end_time"):
        raise HTTPException(status_code=422, detail="وقت البداية والنهاية مطلوبان")

    # Crew must belong to this company.
    crew = sb.table("crew").select("id").eq("id", crew_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Crew member", crew_id)

    st = (data.get("standby_type") or "AIRPORT_STANDBY").upper()
    if st not in _VALID_TYPES:
        st = "AIRPORT_STANDBY"

    row = {
        "id":               str(uuid.uuid4()),
        "company_id":       current_user["company_id"],
        "crew_id":          crew_id,
        "standby_type":     st,
        "airport_code":     (data.get("airport_code") or "").upper() or None,
        "start_time":       data["start_time"],
        "end_time":         data["end_time"],
        "response_minutes": int(data.get("response_minutes") or 60),
        "status":           "ACTIVE",
        "called_out":       False,
        "notes":            data.get("notes"),
        "created_by":       current_user["id"],
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "updated_at":       datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = sb.table("standby_assignments").insert(row).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"تعذّر إنشاء الاحتياط: {str(e)[:200]}")

    write_audit(sb, current_user, "standby_created", "standby", row["id"],
                after={"crew_id": crew_id, "standby_type": st,
                       "airport_code": row["airport_code"],
                       "start_time": row["start_time"], "end_time": row["end_time"],
                       "response_minutes": row["response_minutes"]})
    return res.data[0] if res.data else row


@router.post("/{standby_id}/callout")
async def callout_standby(standby_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Call a reserve out. Optionally links the flight they were called for.
    The actual crew↔flight assignment still goes through /assignments so the
    full compliance gate applies."""
    _ensure_manager(current_user)
    existing = sb.table("standby_assignments").select("*").eq("id", standby_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Standby", standby_id)
    before = existing.data[0]

    flight_id = data.get("flight_id")
    update = {
        "called_out": True,
        "status":     "ASSIGNED" if flight_id else "CALLED_OUT",
        "assigned_flight_id": flight_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    res = sb.table("standby_assignments").update(update).eq("id", standby_id).execute()

    write_audit(sb, current_user, "standby_called_out", "standby", standby_id,
                before={"status": before.get("status"),
                        "called_out": before.get("called_out"),
                        "assigned_flight_id": before.get("assigned_flight_id")},
                after={"status": update["status"], "called_out": True,
                       "assigned_flight_id": flight_id})

    # R1: notify the reserve — ONCE, only on the real ACTIVE→called transition.
    # A retry finds called_out already True; a cancelled/expired reserve isn't
    # ACTIVE — neither re-notifies. Fail-soft: notify never breaks the callout.
    if not before.get("called_out") and before.get("status") == "ACTIVE":
        _notify_reserve_callout(sb, current_user["company_id"], before, flight_id)
    return res.data[0] if res.data else {}


@router.post("/{standby_id}/respond")
async def respond_standby(standby_id: str, data: dict,
                          current_user: CurrentUser, sb: SbClient):
    """R2 — the called-out reserve's own answer: accept | reject (with reason).

    On ACCEPT the assignment is created through the EXISTING `assign_crew`
    path — there is NO parallel assignment route, so every safety gate
    (qualification / documents / training / conflict / rest / FDP-FTL / DNP)
    and the existing assignment audit apply unchanged. Acceptance is recorded
    first, but is NOT a successful tasking until `assignment_id` is set; if the
    gate blocks, `assignment_error` records why and the call returns an error.

    Idempotent: re-accepting a linked callout returns the same assignment;
    re-rejecting returns the stored rejection. NO escalation here (that's R3).
    """
    # Crew-facing: only the crew member the callout is FOR may respond.
    if current_user.get("role") != "crew":
        raise ForbiddenError("هذه النقطة لردّ أفراد الطاقم على الاستدعاء فقط")
    action = str(data.get("action") or "").strip().lower()
    if action not in ("accept", "reject"):
        raise HTTPException(status_code=422,
                            detail="action يجب أن يكون accept أو reject")
    reason = str(data.get("reason") or "").strip()[:300]
    if action == "reject" and not reason:
        raise HTTPException(status_code=422, detail="سبب الرفض مطلوب")

    company_id = current_user["company_id"]
    existing = sb.table("standby_assignments").select("*").eq("id", standby_id) \
        .eq("company_id", company_id).execute()
    if not existing.data:                       # also blocks cross-company access
        raise NotFoundError("Standby", standby_id)
    row = existing.data[0]
    if current_user.get("crew_id") != row.get("crew_id"):
        raise ForbiddenError("لا يمكنك الرد على استدعاء فرد آخر")
    if not row.get("called_out") or row.get("status") not in ("CALLED_OUT", "ASSIGNED"):
        raise HTTPException(status_code=409, detail="لا يوجد استدعاء فعّال للرد عليه")

    now_iso = datetime.now(timezone.utc).isoformat()
    prev = row.get("response_status")

    # ── REJECT ───────────────────────────────────────────────────────────────
    if action == "reject":
        if prev == "REJECTED":                  # idempotent
            return {"ok": True, "response_status": "REJECTED",
                    "responded_at": row.get("responded_at")}
        if prev == "ACCEPTED":
            raise HTTPException(status_code=409,
                                detail="سبق قبول هذا الاستدعاء — لا يمكن رفضه")
        sb.table("standby_assignments").update({
            "response_status": "REJECTED", "response_reason": reason,
            "responded_at": now_iso, "updated_at": now_iso,
        }).eq("id", standby_id).execute()
        write_audit(sb, current_user, "standby_response", "standby", standby_id,
                    before={"response_status": prev},
                    after={"response_status": "REJECTED", "action": "reject"},
                    reason=reason)
        _notify_schedulers_standby(sb, company_id, row, "rejected", reason)
        return {"ok": True, "response_status": "REJECTED", "responded_at": now_iso}

    # ── ACCEPT ───────────────────────────────────────────────────────────────
    if prev == "ACCEPTED" and row.get("assignment_id"):   # idempotent success
        return {"ok": True, "response_status": "ACCEPTED",
                "assignment_id": row.get("assignment_id"),
                "responded_at": row.get("responded_at")}
    if prev == "REJECTED":
        raise HTTPException(status_code=409, detail="سبق رفض هذا الاستدعاء")
    flight_id = row.get("assigned_flight_id")
    if not flight_id:
        raise HTTPException(status_code=422,
                            detail="لا توجد رحلة مرتبطة بالاستدعاء للتعيين")

    # Record acceptance FIRST (so the intent + time are stored even if the
    # downstream assignment later fails). Acceptance ≠ tasking yet.
    if prev != "ACCEPTED":
        sb.table("standby_assignments").update({
            "response_status": "ACCEPTED", "response_reason": None,
            "responded_at": now_iso, "updated_at": now_iso,
        }).eq("id", standby_id).execute()
        write_audit(sb, current_user, "standby_response", "standby", standby_id,
                    before={"response_status": prev},
                    after={"response_status": "ACCEPTED", "action": "accept"})

    # The assignment is performed by the standby OWNER (a scheduler/ops), never
    # by elevating the crew member. If they can't assign, fail loudly.
    assigner = _resolve_assigner(sb, company_id, row.get("created_by"))
    if assigner is None:
        err = "تعذّر التعيين: مُصدِر الاحتياط غير متاح أو لا يملك صلاحية التعيين"
        sb.table("standby_assignments").update(
            {"assignment_error": err, "updated_at": now_iso}).eq("id", standby_id).execute()
        write_audit(sb, current_user, "standby_assign_failed", "standby", standby_id,
                    after={"error": err})
        _notify_schedulers_standby(sb, company_id, row, "accept_failed", err)
        raise HTTPException(status_code=409, detail=err)

    # SAME path as a manual assignment — all gates + the assignment audit run.
    from app.api.v1.endpoints.assignments import assign_crew
    try:
        saved = await assign_crew(
            {"flight_id": flight_id, "crew_id": row["crew_id"],
             "duty_type": "operating"},
            current_user=assigner, sb=sb)
    except ConflictError:
        # Already assigned (e.g. a retry after the assignment was created but
        # before we linked it) — find it and link. No duplicate is created.
        ex = (sb.table("assignments").select("id")
              .eq("flight_id", flight_id).eq("crew_id", row["crew_id"])
              .execute().data) or []
        aid = ex[0]["id"] if ex else None
        sb.table("standby_assignments").update({
            "assignment_id": aid, "assignment_error": None,
            "status": "ASSIGNED", "updated_at": now_iso,
        }).eq("id", standby_id).execute()
        return {"ok": True, "response_status": "ACCEPTED",
                "assignment_id": aid, "idempotent": True}
    except Exception as e:
        detail = getattr(e, "detail", None) or str(e)
        detail = str(detail)[:300]
        sb.table("standby_assignments").update(
            {"assignment_error": detail, "updated_at": now_iso}).eq("id", standby_id).execute()
        write_audit(sb, current_user, "standby_assign_failed", "standby", standby_id,
                    after={"error": detail})
        _notify_schedulers_standby(sb, company_id, row, "accept_failed", detail)
        raise HTTPException(
            status_code=409, detail=f"قُبِل الاستدعاء لكن تعذّر التعيين: {detail}")

    # Success — link the created assignment, clear any prior error.
    sb.table("standby_assignments").update({
        "assignment_id": saved.get("id"), "assignment_error": None,
        "status": "ASSIGNED", "updated_at": now_iso,
    }).eq("id", standby_id).execute()
    write_audit(sb, current_user, "standby_assigned", "standby", standby_id,
                after={"assignment_id": saved.get("id"), "flight_id": flight_id})
    _notify_schedulers_standby(sb, company_id, row, "accepted_assigned")
    return {"ok": True, "response_status": "ACCEPTED",
            "assignment_id": saved.get("id")}


@router.post("/{standby_id}/cancel")
async def cancel_standby(standby_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_manager(current_user)
    existing = sb.table("standby_assignments").select("*").eq("id", standby_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Standby", standby_id)
    before = existing.data[0]
    res = sb.table("standby_assignments").update({
        "status": "CANCELLED",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", standby_id).execute()

    write_audit(sb, current_user, "standby_cancelled", "standby", standby_id,
                before={"status": before.get("status")},
                after={"status": "CANCELLED"})
    return res.data[0] if res.data else {}


@router.delete("/{standby_id}", status_code=204)
async def delete_standby(standby_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_manager(current_user)
    existing = sb.table("standby_assignments").select("*").eq("id", standby_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Standby", standby_id)
    before = existing.data[0]
    sb.table("standby_assignments").delete().eq("id", standby_id).execute()

    write_audit(sb, current_user, "standby_deleted", "standby", standby_id,
                before=before,
                after={"deleted_standby": {
                    "crew_id": before.get("crew_id"),
                    "standby_type": before.get("standby_type"),
                    "status": before.get("status"),
                    "start_time": before.get("start_time"),
                    "end_time": before.get("end_time"),
                }})


@router.get("/suggest/{flight_id}")
async def suggest_standby(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Rank ACTIVE standby crew for a flight: compliant (incl. FDP) first,
    then by fastest response time. Used when a flight is short-crewed."""
    _ensure_manager(current_user)
    fl = sb.table("flights").select("*").eq("id", flight_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not fl.data:
        raise NotFoundError("Flight", flight_id)
    flight = fl.data[0]

    now = datetime.now(timezone.utc)
    dep = _parse_dt(flight.get("departure_time"))
    arr = _parse_dt(flight.get("arrival_time"))
    intl = (flight.get("origin_code", "").upper() not in IRAQI_AIRPORTS or
            flight.get("destination_code", "").upper() not in IRAQI_AIRPORTS)

    rows = sb.table("standby_assignments").select("*") \
        .eq("company_id", current_user["company_id"]).eq("status", "ACTIVE") \
        .execute().data or []
    # Keep only ACTIVE reserves whose window covers the departure. Cancelled /
    # called-out / assigned / expired rows are never valid candidates, and a
    # reserve whose window already ended (relative to NOW) is treated as expired
    # even if the periodic sweep hasn't marked it yet (lazy expiry).
    candidates = []
    for r in rows:
        if r.get("status") != "ACTIVE":
            continue
        s, e = _parse_dt(r.get("start_time")), _parse_dt(r.get("end_time"))
        if e and e < now:
            continue
        if dep and s and e and not (s <= dep <= e):
            continue
        candidates.append(r)

    engine = ComplianceEngine(sb)
    out = []
    for r in _enrich(sb, current_user["company_id"], candidates):
        result = engine.check_crew(
            crew_id=r["crew_id"], flight_id=flight_id,
            flight_departure=dep, flight_arrival=arr, is_international=intl,
            flight_aircraft_type=flight.get("aircraft_type"),
        )
        out.append({
            **r,
            "compliance_status": result.get("status"),
            "blocking_reasons":  result.get("blocking_reasons", []),
        })
    # Compliant first, then fastest response.
    out.sort(key=lambda x: (
        0 if x.get("compliance_status") not in ("BLOCKED", "RED") else 1,
        int(x.get("response_minutes") or 9999),
    ))
    return {"flight_id": flight_id, "candidates": out}


# ──────────────────────────────────────────────────────────────────────
# Expiry — R0: clean, deterministic, NO escalation.
# An ACTIVE reserve whose window has ended is stale. The sweep flips it to
# EXPIRED (audited) so it leaves the active pool and reporting is honest.
# It NEVER touches CALLED_OUT / ASSIGNED / CANCELLED / already-EXPIRED rows,
# and never creates an assignment or sends a notification — those are R1/R2+.
# ──────────────────────────────────────────────────────────────────────

def _expire_company_standby(sb, company_id: str, actor: Optional[dict] = None) -> dict:
    """Flip ACTIVE reserves past their end_time to EXPIRED for one company.
    Idempotent: a second run finds nothing (the rows are no longer ACTIVE)."""
    now = datetime.now(timezone.utc)
    rows = sb.table("standby_assignments").select("*") \
        .eq("company_id", company_id).eq("status", "ACTIVE").execute().data or []
    expired_ids = []
    for r in rows:
        # Belt-and-suspenders: recording fakes ignore .eq filters, and only
        # truly-ended ACTIVE rows may expire.
        if r.get("status") != "ACTIVE":
            continue
        e = _parse_dt(r.get("end_time"))
        if e is None or e >= now:
            continue
        sb.table("standby_assignments").update({
            "status": "EXPIRED",
            "updated_at": now.isoformat(),
        }).eq("id", r["id"]).execute()
        write_audit(sb, actor, "standby_expired", "standby", r["id"],
                    before={"status": "ACTIVE", "end_time": r.get("end_time")},
                    after={"status": "EXPIRED"},
                    company_id=company_id)
        expired_ids.append(r["id"])
    return {"expired": len(expired_ids), "ids": expired_ids}


@router.post("/expire")
async def expire_standby_now(current_user: CurrentUser, sb: SbClient):
    """Manual trigger (supervisors) — expire stale reserves for THIS company."""
    _ensure_supervisor(current_user)
    return _expire_company_standby(sb, current_user["company_id"], current_user)


@router.get("/cron/expire", status_code=200)
async def cron_expire_standby(sb: SbClient,
                              authorization: Optional[str] = Header(default=None)):
    """Scheduled trigger (Vercel Cron → GET, `Authorization: Bearer
    $CRON_SECRET`) — expire stale reserves for EVERY active company."""
    secret = getattr(settings, "CRON_SECRET", "") or ""
    if not secret or authorization != f"Bearer {secret}":
        raise ForbiddenError("Invalid cron credentials")
    companies = (sb.table("companies").select("id")
                 .eq("is_active", True).execute().data) or []
    results = {}
    for c in companies:
        try:
            results[c["id"]] = _expire_company_standby(
                sb, c["id"],
                {"id": "system", "name_en": "standby-cron", "company_id": c["id"]})
        except Exception as e:
            logger.warning("standby expiry cron failed for company %s: %s",
                           c.get("id"), e)
            results[c["id"]] = {"error": str(e)[:120]}
    return {"companies": len(companies), "results": results}
