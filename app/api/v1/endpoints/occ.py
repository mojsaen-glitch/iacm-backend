"""Operations Control Center (OCC) — single aggregated snapshot.

OCC used to fire ~10 requests every minute (flights + all assignments + all crew
+ fdp-today + blocked-crew, several paginated). At thousands of concurrent users
that's a lot of round-trips. This endpoint returns the WHOLE board in ONE call,
computed server-side from BOUNDED queries (today's roster + fdp-today, never a
full per-crew compliance scan), so it stays fast at scale.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.core.audit import write_audit
from app.core.exceptions import ForbiddenError, NotFoundError
from app.services import push_service
from app.api.v1.endpoints.flights import (
    _normalize_reg, _validate_reg_format, mark_gd_stale_if_finalized,
)

router = APIRouter(prefix="/occ", tags=["OCC"])

_BAGHDAD = timedelta(hours=3)
_FINAL = {"arrived", "landed", "completed", "cancelled"}

# Roles allowed to take operational (flight-data-changing) OCC actions.
_OCC_DELAY_ROLES = {
    "super_admin", "admin", "ops_manager",
    "flight_movement", "flight_movement_admin",
    "flight_operations", "flight_operations_admin", "flight_ops",
}
# A flight in any of these states can no longer be delayed.
_DELAY_BLOCKED_STATUSES = {
    "cancelled", "departed", "in_air", "landed", "arrived", "completed", "diverted",
}
# Allowed delay reason codes (UI dropdown must match these).
_DELAY_REASON_CODES = {
    "weather", "technical", "crew_shortage", "operational",
    "commercial", "atc", "security", "other",
}
# Allowed aircraft-change reason codes.
_AIRCRAFT_CHANGE_REASONS = {
    "maintenance", "aog", "capacity", "swap", "operational", "other",
}
# Roles to alert when an aircraft change leaves crew un-rated (must review/replace).
_CREW_REVIEW_ROLES = {
    "super_admin", "admin", "ops_manager",
    "scheduler", "scheduler_admin", "flight_movement", "flight_movement_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}


def _norm_type(v) -> str:
    """Alphanumeric-only upper form of an aircraft type/qualification token."""
    return "".join(ch for ch in str(v or "").upper() if ch.isalnum())


def _crew_qualified(crew_row: dict, ac_type: str) -> bool:
    """Tolerant type-rating check (B738 ≈ 738) — same spirit as the FDC rule.
    Only a WARNING signal for Change Aircraft; it never blocks or moves crew."""
    target = _norm_type(ac_type)
    if not target:
        return True
    cands: list = []
    quals = crew_row.get("aircraft_qualifications")
    if isinstance(quals, list):
        cands = list(quals)
    elif isinstance(quals, str) and quals.strip():
        try:
            parsed = json.loads(quals)
            cands = parsed if isinstance(parsed, list) else [quals]
        except Exception:
            cands = re.split(r"[,;|]", quals)
    for extra in (crew_row.get("aircraft_type"), crew_row.get("fleet")):
        if extra:
            cands.append(extra)
    norm = [_norm_type(q) for q in cands]
    norm = [q for q in norm if q]
    if not norm:
        return True   # no qualification data on file → can't assert un-rated (no false alarm)
    return any(qn == target or qn in target or target in qn for qn in norm)
# Cheap FDP estimate ceiling (minutes). The EXACT, rule-based FDP lives on the
# dedicated FDP-monitor page; the OCC board only needs a fast at-a-glance number,
# so it sums today's block hours instead of running the per-crew engine (which
# fetches every company flight + many queries per crew → too slow for a live
# board polled every minute at scale).
_MAX_FDP_MIN = 13 * 60

# OCC exposes FDP/compliance (sensitive) — restrict to ops/scheduling/compliance.
_OCC_READERS = {
    "super_admin", "admin", "ops_manager", "scheduler", "scheduler_admin",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "compliance_officer", "flight_movement", "flight_movement_admin",
    "flight_ops", "flight_operations", "flight_operations_admin",
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}


def _ensure_occ_reader(user: dict) -> None:
    if user.get("role") not in _OCC_READERS and not user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بعرض مركز العمليات")


def _op_for(crew_status, statuses: list, rest_hours) -> str:
    """Operational state from a crew's today flight statuses + rest."""
    if crew_status == "blocked":
        return "blocked"
    if statuses:
        if any(s in ("departed", "in_air") for s in statuses):
            return "in_flight"
        if any(s == "boarding" for s in statuses):
            return "boarding"
        if all(s in _FINAL for s in statuses):
            return "landed"
        return "assigned"
    return "resting" if (rest_hours or 0) > 0 else "available"


@router.get("/snapshot")
async def occ_snapshot(current_user: CurrentUser, sb: SbClient):
    _ensure_occ_reader(current_user)
    cid = current_user["company_id"]

    now_utc = datetime.now(timezone.utc)
    bag = now_utc + _BAGHDAD
    start_utc = datetime(bag.year, bag.month, bag.day, tzinfo=timezone.utc) - _BAGHDAD
    end_utc = start_utc + timedelta(days=1)

    # 1) Today's flights (Baghdad day window).
    frows = (sb.table("flights").select("*").eq("company_id", cid)
             .gte("departure_time", start_utc.isoformat())
             .lt("departure_time", end_utc.isoformat())
             .order("departure_time").execute().data) or []
    flight_ids = [f["id"] for f in frows if f.get("id")]
    status_by_fid = {f["id"]: (f.get("status") or "scheduled") for f in frows}
    label_by_fid = {
        f["id"]: f"{f.get('flight_number', '')} {f.get('origin_code', '')}→{f.get('destination_code', '')}"
        for f in frows
    }

    # 2) Assignments for today's flights → counts + crew→flights + flight→crew.
    asg = []
    if flight_ids:
        asg = (sb.table("assignments").select("crew_id,flight_id,duty_type")
               .in_("flight_id", flight_ids).execute().data) or []
    count_by_fid: dict = {}
    duty_by_crew: dict = {}
    op_crew_by_fid: dict = {}   # flight → operating crew ids (for qualification check)
    for a in asg:
        fid = a.get("flight_id")
        ccid = a.get("crew_id")
        if fid:
            count_by_fid[fid] = count_by_fid.get(fid, 0) + 1
        if ccid and fid in status_by_fid:
            duty_by_crew.setdefault(ccid, []).append(fid)
            if (a.get("duty_type") or "operating") == "operating":
                op_crew_by_fid.setdefault(fid, []).append(ccid)

    # 3) Crew rows: on-duty ∪ blocked (BOUNDED — never the whole company).
    cols = ("id,full_name_ar,full_name_en,rank,status,rest_hours_due,operator_company_id,"
            "last_flight_date,aircraft_qualifications")
    on_duty_ids = list(duty_by_crew.keys())
    crew_by_id: dict = {}
    if on_duty_ids:
        for c in (sb.table("crew").select(cols).in_("id", on_duty_ids).execute().data or []):
            crew_by_id[c["id"]] = c
    blocked_rows = (sb.table("crew").select(cols)
                    .eq("company_id", cid).eq("status", "blocked").execute().data) or []
    blocked_ids = set()
    for c in blocked_rows:
        crew_by_id.setdefault(c["id"], c)
        blocked_ids.add(c["id"])

    # 4) Per-flight block hours (for the cheap FDP estimate — no engine call).
    dur_by_fid = {f["id"]: float(f.get("duration_hours") or 0) for f in frows if f.get("id")}

    # 5) Flights payload (with LIVE crew-revalidation: any operating crew not
    #    type-rated for the flight's CURRENT aircraft → crew_review_required.
    #    Derived each snapshot, so it self-clears once qualified crew are set).
    flights_out = []
    in_air = delayed = 0
    for f in frows:
        st = f.get("status") or "scheduled"
        if st in ("departed", "in_air"):
            in_air += 1
        if st == "delayed":
            delayed += 1
        fid = f.get("id")
        ac_type = f.get("aircraft_type") or ""
        unqualified = 0
        if ac_type and fid in op_crew_by_fid and st not in _FINAL:
            for ccid in op_crew_by_fid[fid]:
                if not _crew_qualified(crew_by_id.get(ccid, {}), ac_type):
                    unqualified += 1
        flights_out.append({
            "id": fid,
            "flight_number": f.get("flight_number", ""),
            "origin_code": f.get("origin_code", ""),
            "destination_code": f.get("destination_code", ""),
            "departure_time": f.get("departure_time"),
            "arrival_time": f.get("arrival_time"),
            "status": st,
            "delay_minutes": f.get("delay_minutes") or 0,
            "estimated_departure_time": f.get("estimated_departure_time"),
            "delay_reason_code": f.get("delay_reason_code") or "",
            "aircraft_registration": f.get("aircraft_registration") or "",
            "aircraft_type": ac_type,
            "crew_count": count_by_fid.get(fid, 0),
            "crew_required": f.get("crew_required") or 0,
            "unqualified_crew": unqualified,
            "crew_review_required": unqualified > 0,
            # Roster lifecycle — so the UI can show draft/published/GD without
            # confusing it with the OPERATIONAL status above.
            "publish_status": f.get("publish_status") or "draft",
            "roster_finalized_status": f.get("roster_finalized_status") or "",
            "gd_status": f.get("gd_status") or "",
        })

    # 6) Crew operational cards.
    def current_flight(ccid: str) -> str:
        fids = duty_by_crew.get(ccid, [])
        for fid in fids:
            if status_by_fid.get(fid) in ("departed", "in_air", "boarding"):
                return label_by_fid.get(fid, "")
        for fid in fids:
            if status_by_fid.get(fid) not in _FINAL:
                return label_by_fid.get(fid, "")
        return label_by_fid.get(fids[0], "") if fids else ""

    crew_out = []
    for ccid in set(on_duty_ids) | blocked_ids:
        c = crew_by_id.get(ccid, {})
        name_ar = (c.get("full_name_ar") or "").strip()
        name_en = (c.get("full_name_en") or "").strip()
        if not (name_ar or name_en):
            continue
        fids = duty_by_crew.get(ccid, [])
        statuses = [status_by_fid.get(fid) for fid in fids]
        fdp_used_min = int(round(sum(dur_by_fid.get(fid, 0) for fid in fids) * 60))
        if c.get("status") == "blocked":
            verdict = "BLOCKED"
        elif fdp_used_min > _MAX_FDP_MIN:
            verdict = "RED"
        elif fdp_used_min >= 0.8 * _MAX_FDP_MIN:
            verdict = "YELLOW"
        else:
            verdict = "GREEN"
        crew_out.append({
            "crew_id": ccid,
            "name_ar": name_ar,
            "name_en": name_en,
            "rank": c.get("rank") or "",
            "company": c.get("operator_company_id") or "",
            "op": _op_for(c.get("status"), statuses, c.get("rest_hours_due")),
            "fdp_used_minutes": fdp_used_min,
            "fdp_max_minutes": _MAX_FDP_MIN,
            "verdict": verdict,
            "sectors": len(fids),
            "current_flight": current_flight(ccid),
            "last_flight": (str(c.get("last_flight_date"))[:10] if c.get("last_flight_date") else ""),
            "reason": "",
        })

    # 7) Categorized alerts.
    alerts = []
    for c in crew_out:
        nm = c["name_ar"] or c["name_en"]
        if c["verdict"] == "BLOCKED":
            alerts.append({"severity": "critical", "crew_name": nm, "message": "طاقم محظور", "kind": "blocked"})
        elif c["verdict"] == "RED":
            alerts.append({"severity": "critical", "crew_name": nm, "message": "تجاوز تقديري لحد FDP", "kind": "fdp"})
        elif c["verdict"] == "YELLOW":
            alerts.append({"severity": "warning", "crew_name": nm, "message": "اقتراب من حد FDP", "kind": "fdp"})
    for f in flights_out:
        req, cnt, st = f["crew_required"], f["crew_count"], f["status"]
        if req > 0 and cnt < req and st not in ("cancelled", "arrived", "landed"):
            alerts.append({"severity": "critical", "crew_name": f["flight_number"],
                           "message": f"طاقم ناقص ({cnt}/{req})", "kind": "understaffed"})
        if f.get("crew_review_required"):
            alerts.append({"severity": "critical", "crew_name": f["flight_number"],
                           "message": f"طاقم غير مؤهل للطراز الجديد ({f['unqualified_crew']}) — تحتاج مراجعة الطاقم",
                           "kind": "crew_review"})
        if st == "delayed":
            alerts.append({"severity": "warning", "crew_name": f["flight_number"],
                           "message": "رحلة متأخرة", "kind": "delay"})
        if st in ("arrived", "landed"):
            alerts.append({"severity": "info", "crew_name": f["flight_number"],
                           "message": f"{f['origin_code']}→{f['destination_code']}", "kind": "landed"})

    # 8) Counts.
    try:
        active_total = (sb.table("crew").select("id", count="estimated")
                        .eq("company_id", cid).eq("status", "active").execute().count) or 0
    except Exception:
        active_total = 0
    available = max(0, active_total - len(set(on_duty_ids) | blocked_ids))
    crew_on_duty = sum(1 for c in crew_out if c["op"] in ("in_flight", "boarding", "assigned"))
    critical = sum(1 for a in alerts if a["severity"] == "critical")

    # 9) Company id→name (so the UI filter shows names, never raw UUIDs).
    companies_out = []
    comp_ids = {c["company"] for c in crew_out if c.get("company")}
    if comp_ids:
        crows = (sb.table("companies").select("id,name_ar,name_en,code")
                 .in_("id", list(comp_ids)).execute().data) or []
        companies_out = [{
            "id": r.get("id"),
            "name_ar": (r.get("name_ar") or r.get("name_en") or r.get("code") or ""),
            "name_en": (r.get("name_en") or r.get("name_ar") or r.get("code") or ""),
        } for r in crows if r.get("id")]

    return {
        "last_updated": now_utc.isoformat(),
        "kpis": {
            "flights_today": len(flights_out), "in_air": in_air, "delayed": delayed,
            "crew_on_duty": crew_on_duty, "available": available, "critical_alerts": critical,
        },
        "flights": flights_out,
        "crew": crew_out,
        "alerts": alerts,
        "companies": companies_out,
    }


def _parse_dt(v):
    """UTC-aware datetime from an ISO string (assumes UTC if naive)."""
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@router.post("/flights/{flight_id}/delay")
async def occ_delay_flight(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """OCC Advanced Delay — sets the new ETD (estimated_departure_time) WITHOUT
    touching the scheduled STD. Validates status + ETD, requires a reason code,
    records delay metadata, audits, optionally notifies, and returns an impact
    summary."""
    if current_user.get("role") not in _OCC_DELAY_ROLES and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بتأخير الرحلة")
    cid = current_user["company_id"]

    res = (sb.table("flights").select("*")
           .eq("id", flight_id).eq("company_id", cid).execute())
    if not res.data:
        raise NotFoundError("Flight", flight_id)
    flight = res.data[0]

    status = (flight.get("status") or "scheduled").lower()
    if status in _DELAY_BLOCKED_STATUSES:
        raise HTTPException(status_code=409, detail=f"لا يمكن تأخير رحلة بحالة '{status}'")

    std = _parse_dt(flight.get("departure_time"))
    if std is None:
        raise HTTPException(status_code=422, detail="الموعد المجدول (STD) للرحلة غير صالح")

    if data.get("new_etd") in (None, ""):
        raise HTTPException(status_code=422, detail="وقت ETD الجديد مطلوب")
    etd = _parse_dt(data.get("new_etd"))
    if etd is None:
        raise HTTPException(status_code=422, detail="صيغة وقت ETD غير صحيحة")

    now = datetime.now(timezone.utc)
    if etd <= std:
        raise HTTPException(status_code=422, detail="يجب أن يكون ETD بعد الموعد المجدول STD")
    if etd <= now:
        raise HTTPException(status_code=422, detail="يجب أن يكون ETD بعد الوقت الحالي")

    reason_code = str(data.get("reason_code") or "").strip().lower()
    if not reason_code:
        raise HTTPException(status_code=422, detail="سبب التأخير (reason_code) مطلوب")
    if reason_code not in _DELAY_REASON_CODES:
        raise HTTPException(status_code=422,
                            detail=f"سبب التأخير يجب أن يكون أحد: {', '.join(sorted(_DELAY_REASON_CODES))}")
    notes = str(data.get("notes") or "").strip()[:500]

    delay_minutes = int(round((etd - std).total_seconds() / 60))
    now_iso = now.isoformat()
    # STD (departure_time) is intentionally NOT in this update.
    update = {
        "status": "delayed",
        "estimated_departure_time": etd.isoformat(),
        "delay_minutes": delay_minutes,
        "delay_reason_code": reason_code,
        "delay_notes": notes or None,
        "delay_updated_at": now_iso,
        "delay_updated_by": current_user["id"],
        "updated_at": now_iso,
    }
    sb.table("flights").update(update).eq("id", flight_id).execute()

    # ── Impact: affected operating crew + same-tail downstream flights. ──
    asg = (sb.table("assignments").select("crew_id, duty_type")
           .eq("flight_id", flight_id).execute().data) or []
    operating_crew_ids = [a["crew_id"] for a in asg
                          if a.get("crew_id") and (a.get("duty_type") or "operating") == "operating"]
    crew_affected = len(operating_crew_ids)

    reg = (flight.get("aircraft_registration") or "").strip()
    downstream = 0
    if reg:
        tail = (sb.table("flights").select("id,departure_time,status")
                .eq("company_id", cid).eq("aircraft_registration", reg).execute().data) or []
        for tf in tail:
            if tf.get("id") == flight_id or (tf.get("status") or "").lower() in _FINAL:
                continue
            tdep = _parse_dt(tf.get("departure_time"))
            if tdep is not None and tdep > std:
                downstream += 1

    # ── Optional notifications (crew and/or operations). ──
    notify_crew = bool(data.get("notify_crew", True))
    notify_ops = bool(data.get("notify_ops", False))
    fnum = flight.get("flight_number", "")
    origin = flight.get("origin_code", "")
    dest = flight.get("destination_code", "")
    title_ar = f"تأخّر الرحلة {fnum}"
    msg_ar = f"رحلة {fnum} ({origin} → {dest}) تأخّرت {delay_minutes} دقيقة" + (f" — {notes}" if notes else "")
    targets: set = set()
    if notify_crew and operating_crew_ids:
        urows = (sb.table("users").select("id,crew_id").eq("company_id", cid)
                 .in_("crew_id", operating_crew_ids).execute().data) or []
        targets.update(u["id"] for u in urows if u.get("id"))
    if notify_ops:
        ops = (sb.table("users").select("id,role").eq("company_id", cid)
               .eq("is_active", True).execute().data) or []
        targets.update(u["id"] for u in ops if u.get("role") in _OCC_DELAY_ROLES)
    targets = list(targets)
    if targets:
        try:
            sb.table("notifications").insert([{
                "id": str(uuid.uuid4()), "user_id": uid, "type": "flight_disruption",
                "title_ar": title_ar, "title_en": f"Flight {fnum} delayed",
                "message_ar": msg_ar, "message_en": f"Flight {fnum} ({origin}→{dest}) delayed {delay_minutes} min",
                "body_ar": msg_ar, "body_en": f"Flight {fnum} delayed {delay_minutes} min",
                "reference_id": flight_id, "reference_type": "flight",
                "related_flight_id": flight_id, "is_read": False, "created_at": now_iso,
            } for uid in targets]).execute()
            push_service.send_to_users(sb, targets, title=title_ar, body=msg_ar,
                                       data={"type": "flight_disruption",
                                             "reference_id": str(flight_id),
                                             "reference_type": "flight"})
        except Exception as e:
            logging.getLogger(__name__).warning("occ delay notify failed: %s", e)

    # ── Audit. ──
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "occ_delay_flight", "entity_type": "flight", "entity_id": flight_id,
            "company_id": cid, "created_at": now_iso,
            "after_data": json.dumps({
                "flight_number": fnum, "delay_minutes": delay_minutes,
                "reason_code": reason_code, "notes": notes or None,
                "new_etd": etd.isoformat(), "crew_affected": crew_affected,
                "downstream_same_tail": downstream, "recipients": len(targets),
            }, ensure_ascii=False),
        }).execute()
    except Exception as e:
        logging.getLogger(__name__).warning("occ delay audit failed: %s", e)

    # New ETA = scheduled arrival shifted by the same delay.
    sta = _parse_dt(flight.get("arrival_time"))
    new_eta = (sta + timedelta(minutes=delay_minutes)).isoformat() if sta is not None else None

    return {
        "ok": True,
        "flight": {
            "id": flight_id, "flight_number": fnum, "status": "delayed",
            "std": flight.get("departure_time"), "etd": etd.isoformat(), "eta": new_eta,
            "delay_minutes": delay_minutes, "delay_reason_code": reason_code,
        },
        "impact": {
            "delay_minutes": delay_minutes, "crew_affected": crew_affected,
            "downstream_same_tail": downstream, "notified": len(targets), "new_eta": new_eta,
        },
    }


@router.post("/flights/{flight_id}/change-aircraft")
async def occ_change_aircraft(flight_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """OCC Change Aircraft — swaps the flight's tail (REG) and/or type. Marks the
    GenDec stale (REG changed) and reports a crew type-rating impact, but NEVER
    moves crew (that is Replace Crew, a later phase). Change history lives in the
    audit log, so no new flight columns are needed."""
    if current_user.get("role") not in _OCC_DELAY_ROLES and not current_user.get("is_superuser"):
        raise ForbiddenError("غير مصرح بتغيير طائرة الرحلة")
    cid = current_user["company_id"]

    res = (sb.table("flights").select("*")
           .eq("id", flight_id).eq("company_id", cid).execute())
    if not res.data:
        raise NotFoundError("Flight", flight_id)
    flight = res.data[0]

    status = (flight.get("status") or "scheduled").lower()
    if status in _DELAY_BLOCKED_STATUSES:
        raise HTTPException(status_code=409, detail=f"لا يمكن تغيير طائرة رحلة بحالة '{status}'")

    new_reg = _normalize_reg(data.get("aircraft_registration"))
    if not new_reg:
        raise HTTPException(status_code=422, detail="رقم تسجيل الطائرة (REG) مطلوب")
    _validate_reg_format(new_reg)

    old_reg = _normalize_reg(flight.get("aircraft_registration"))
    old_type = str(flight.get("aircraft_type") or "").strip()
    new_type = str(data.get("aircraft_type") or "").strip() or old_type
    if new_reg == old_reg and _norm_type(new_type) == _norm_type(old_type):
        raise HTTPException(status_code=422, detail="لا يوجد تغيير في الطائرة")

    reason_code = str(data.get("reason_code") or "").strip().lower()
    if not reason_code:
        raise HTTPException(status_code=422, detail="سبب التغيير (reason_code) مطلوب")
    if reason_code not in _AIRCRAFT_CHANGE_REASONS:
        raise HTTPException(status_code=422,
                            detail=f"سبب التغيير يجب أن يكون أحد: {', '.join(sorted(_AIRCRAFT_CHANGE_REASONS))}")
    notes = str(data.get("notes") or "").strip()[:500]

    now_iso = datetime.now(timezone.utc).isoformat()
    sb.table("flights").update({
        "aircraft_registration": new_reg,
        "aircraft_type": new_type,
        "updated_at": now_iso,
    }).eq("id", flight_id).execute()

    # ── Impact: assigned operating crew + type-rating mismatch for the new type. ──
    asg = (sb.table("assignments").select("crew_id, duty_type")
           .eq("flight_id", flight_id).execute().data) or []
    operating_crew_ids = [a["crew_id"] for a in asg
                          if a.get("crew_id") and (a.get("duty_type") or "operating") == "operating"]
    crew_total = len(operating_crew_ids)
    type_changed = _norm_type(new_type) != _norm_type(old_type)
    unqualified = 0
    if type_changed and operating_crew_ids:
        crows = (sb.table("crew").select("id,aircraft_qualifications")
                 .in_("id", operating_crew_ids).execute().data) or []
        unqualified = sum(1 for c in crows if not _crew_qualified(c, new_type))
    # Crew revalidation: a type change that leaves un-rated crew needs scheduler review.
    crew_review_required = type_changed and unqualified > 0

    # REG change invalidates a finalized GenDec → mark it stale (notifies ops).
    gd_was_ready = (flight.get("gd_status") == "ready"
                    and flight.get("roster_finalized_status") == "finalized")
    try:
        mark_gd_stale_if_finalized(sb, cid, flight_id, actor=current_user)
    except Exception as e:
        logging.getLogger(__name__).warning("occ change-aircraft gd-stale failed: %s", e)

    # ── Optional notifications. ──
    notify_crew = bool(data.get("notify_crew", True))
    notify_ops = bool(data.get("notify_ops", False))
    fnum = flight.get("flight_number", "")
    origin = flight.get("origin_code", "")
    dest = flight.get("destination_code", "")
    title_ar = f"تغيير طائرة الرحلة {fnum}"
    msg_ar = (f"تم تغيير طائرة الرحلة {fnum} ({origin} → {dest}) إلى {new_reg}"
              + (f" / {new_type}" if new_type else "") + (f" — {notes}" if notes else ""))
    targets: set = set()
    if notify_crew and operating_crew_ids:
        urows = (sb.table("users").select("id,crew_id").eq("company_id", cid)
                 .in_("crew_id", operating_crew_ids).execute().data) or []
        targets.update(u["id"] for u in urows if u.get("id"))
    if notify_ops:
        ops = (sb.table("users").select("id,role").eq("company_id", cid)
               .eq("is_active", True).execute().data) or []
        targets.update(u["id"] for u in ops if u.get("role") in _OCC_DELAY_ROLES)
    targets = list(targets)
    if targets:
        try:
            sb.table("notifications").insert([{
                "id": str(uuid.uuid4()), "user_id": uid, "type": "flight_disruption",
                "title_ar": title_ar, "title_en": f"Aircraft changed — {fnum}",
                "message_ar": msg_ar, "message_en": f"Flight {fnum} aircraft changed to {new_reg}",
                "body_ar": msg_ar, "body_en": f"Flight {fnum} aircraft changed to {new_reg}",
                "reference_id": flight_id, "reference_type": "flight",
                "related_flight_id": flight_id, "is_read": False, "created_at": now_iso,
            } for uid in targets]).execute()
            push_service.send_to_users(sb, targets, title=title_ar, body=msg_ar,
                                       data={"type": "flight_disruption",
                                             "reference_id": str(flight_id),
                                             "reference_type": "flight"})
        except Exception as e:
            logging.getLogger(__name__).warning("occ change-aircraft notify failed: %s", e)

    # ── Crew Revalidation: un-rated crew → ALWAYS alert the scheduling roles to
    #    review/replace, regardless of the notify toggles above. ──
    scheduler_notified = 0
    if crew_review_required:
        rev_title = f"مراجعة طاقم مطلوبة — {fnum}"
        rev_msg = (f"تم تغيير طائرة الرحلة {fnum} إلى {new_reg}"
                   + (f" / {new_type}" if new_type else "")
                   + f". يرجى مراجعة الطاقم واختيار بدلاء مؤهلين للطراز الجديد "
                     f"({unqualified} غير مؤهّل).")
        sched = (sb.table("users").select("id,role").eq("company_id", cid)
                 .eq("is_active", True).execute().data) or []
        sched_ids = [u["id"] for u in sched if u.get("role") in _CREW_REVIEW_ROLES and u.get("id")]
        scheduler_notified = len(sched_ids)
        if sched_ids:
            try:
                sb.table("notifications").insert([{
                    "id": str(uuid.uuid4()), "user_id": uid, "type": "crew_review_required",
                    "title_ar": rev_title, "title_en": f"Crew review required — {fnum}",
                    "message_ar": rev_msg,
                    "message_en": f"Flight {fnum} aircraft changed to {new_reg}; review crew — "
                                  f"{unqualified} not type-rated.",
                    "body_ar": rev_msg, "body_en": f"Crew review required — {fnum}",
                    "reference_id": flight_id, "reference_type": "flight",
                    "related_flight_id": flight_id, "is_read": False, "created_at": now_iso,
                } for uid in sched_ids]).execute()
                push_service.send_to_users(sb, sched_ids, title=rev_title, body=rev_msg,
                                           data={"type": "crew_review_required",
                                                 "reference_id": str(flight_id),
                                                 "reference_type": "flight"})
            except Exception as e:
                logging.getLogger(__name__).warning("occ crew-review notify failed: %s", e)

    # ── Audit (carries the full before→after change). ──
    try:
        sb.table("audit_log").insert({
            "user_id": current_user["id"],
            "user_name": current_user.get("name_ar") or current_user.get("name_en")
                         or current_user.get("email", ""),
            "action": "occ_change_aircraft", "entity_type": "flight", "entity_id": flight_id,
            "company_id": cid, "created_at": now_iso,
            "after_data": json.dumps({
                "flight_number": fnum, "reason_code": reason_code, "notes": notes or None,
                "from_reg": old_reg or None, "to_reg": new_reg,
                "from_type": old_type or None, "to_type": new_type or None,
                "type_changed": type_changed, "crew_total": crew_total,
                "crew_unqualified": unqualified, "gd_marked_stale": gd_was_ready,
                "crew_revalidation_required": crew_review_required,
                "unqualified_crew_count": unqualified,
                "scheduler_notified": scheduler_notified > 0,
                "recipients": len(targets),
            }, ensure_ascii=False),
        }).execute()
    except Exception as e:
        logging.getLogger(__name__).warning("occ change-aircraft audit failed: %s", e)

    return {
        "ok": True,
        "flight": {
            "id": flight_id, "flight_number": fnum, "status": status,
            "aircraft_registration": new_reg, "aircraft_type": new_type,
        },
        "impact": {
            "crew_total": crew_total, "crew_unqualified": unqualified,
            "type_changed": type_changed, "gd_marked_stale": gd_was_ready,
            "crew_review_required": crew_review_required,
            "scheduler_notified": scheduler_notified,
            "notified": len(targets),
        },
    }


@router.get("/flights/{flight_id}/crew")
async def occ_flight_crew(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Enriched manifest for the OCC Flight Drawer: each crew member's name +
    rank + duty + whether they are type-rated for the flight's CURRENT aircraft.
    Drives the crew-revalidation badge after a Change Aircraft."""
    _ensure_occ_reader(current_user)
    cid = current_user["company_id"]
    res = (sb.table("flights").select("id,flight_number,aircraft_type,aircraft_registration,"
                                       "actual_departure_time,actual_arrival_time")
           .eq("id", flight_id).eq("company_id", cid).execute())
    if not res.data:
        raise NotFoundError("Flight", flight_id)
    flight = res.data[0]
    ac_type = flight.get("aircraft_type") or ""

    # select * — the drawer's Replace action needs assignment_id, and the row
    # chips need the acceptance fields (status derived below).
    asg = (sb.table("assignments").select("*")
           .eq("flight_id", flight_id).execute().data) or []
    crew_ids = [a["crew_id"] for a in asg if a.get("crew_id")]
    by_id: dict = {}
    if crew_ids:
        cols = "id,full_name_ar,full_name_en,rank,aircraft_qualifications"
        for c in (sb.table("crew").select(cols).in_("id", crew_ids).execute().data or []):
            by_id[c["id"]] = c

    from app.api.v1.endpoints.assignments import _acceptance_status_row
    crew = []
    unqualified = 0
    for a in asg:
        ccid = a.get("crew_id")
        if not ccid:
            continue
        c = by_id.get(ccid, {})
        duty = a.get("duty_type") or "operating"
        operating = duty == "operating"
        qualified = _crew_qualified(c, ac_type) if ac_type else True
        if operating and ac_type and not qualified:
            unqualified += 1
        crew.append({
            "crew_id": ccid,
            "assignment_id": a.get("id"),
            "acceptance_status": _acceptance_status_row(a),
            "name_ar": (c.get("full_name_ar") or "").strip(),
            "name_en": (c.get("full_name_en") or "").strip(),
            "rank": c.get("rank") or "",
            "duty_type": duty,
            "operating": operating,
            "qualified": qualified,
        })

    return {
        "flight_id": flight_id,
        "flight_number": flight.get("flight_number", ""),
        "aircraft_type": ac_type,
        "aircraft_registration": flight.get("aircraft_registration") or "",
        "actual_departure_time": flight.get("actual_departure_time"),
        "actual_arrival_time": flight.get("actual_arrival_time"),
        "crew_review_required": unqualified > 0,
        "unqualified_crew": unqualified,
        "crew": crew,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Actual movement times — ATD/ATA (Phase 1 of the actual-hours model)
# An INDEPENDENT layer: STD/STA stay the schedule, ETD/ETA stay the delay
# estimate. Recorded EXPLICITLY here (never auto-written by status changes).
# First recording: reason optional. EDITING a recorded value: reason MANDATORY.
# Reports use actual when both ATD+ATA exist, scheduled as fallback (engine
# `_leg_hours`). Pre-flight FTL/FDP and the GD gates are untouched.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_actual(value, label):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422,
                            detail=f"صيغة وقت غير صالحة لـ {label}")


@router.post("/flights/{flight_id}/actual-times")
async def record_actual_times(flight_id: str, data: dict,
                              current_user: CurrentUser, sb: SbClient):
    """Record or correct ATD/ATA for a flight (OCC action)."""
    if current_user.get("role") not in _OCC_DELAY_ROLES \
            and not current_user.get("is_superuser"):
        raise ForbiddenError("تسجيل الأوقات الفعلية يتطلب صلاحية عمليات/OCC")
    cid = current_user["company_id"]

    res = (sb.table("flights").select("*")
           .eq("id", flight_id).eq("company_id", cid).execute())
    if not res.data:
        raise NotFoundError("Flight", flight_id)
    flight = res.data[0]
    if "actual_departure_time" not in flight:
        raise HTTPException(
            status_code=422,
            detail="يلزم تشغيل ترحيل قاعدة البيانات "
                   "(migrations/2026_06_12_flight_actual_times.sql)")

    atd_in = data.get("atd")
    ata_in = data.get("ata")
    reason = (data.get("reason") or "").strip()
    if atd_in is None and ata_in is None:
        raise HTTPException(status_code=422, detail="أرسل atd و/أو ata")

    before = {"atd": flight.get("actual_departure_time"),
              "ata": flight.get("actual_arrival_time")}
    update: dict = {}
    editing = False   # changing an already-recorded value ⇒ reason mandatory

    if atd_in is not None:
        atd_dt = _parse_actual(atd_in, "ATD")
        new_atd = atd_dt.isoformat()
        if before["atd"] and str(before["atd"]) != new_atd:
            editing = True
        update["actual_departure_time"] = new_atd
    if ata_in is not None:
        ata_dt = _parse_actual(ata_in, "ATA")
        new_ata = ata_dt.isoformat()
        if before["ata"] and str(before["ata"]) != new_ata:
            editing = True
        update["actual_arrival_time"] = new_ata

    # ATA must follow ATD (whichever pair results after this update).
    final_atd = update.get("actual_departure_time", before["atd"])
    final_ata = update.get("actual_arrival_time", before["ata"])
    if final_atd and final_ata:
        if _parse_actual(final_ata, "ATA") <= _parse_actual(final_atd, "ATD"):
            raise HTTPException(status_code=422,
                                detail="ATA يجب أن يكون بعد ATD")

    if editing and not reason:
        raise HTTPException(status_code=422,
                            detail="تعديل وقت فعلي مسجَّل يتطلب سبباً")

    now_iso = datetime.now(timezone.utc).isoformat()
    update["actual_times_updated_by"] = current_user["id"]
    update["actual_times_updated_at"] = now_iso
    update["updated_at"] = now_iso
    sb.table("flights").update(update).eq("id", flight_id).execute()

    after = {"atd": final_atd, "ata": final_ata}
    write_audit(sb, current_user,
                "actual_times_updated" if editing else "actual_times_recorded",
                "flight", flight_id,
                before=before,
                after={**after, "flight_number": flight.get("flight_number")},
                reason=reason or None)

    actual_block = None
    if final_atd and final_ata:
        actual_block = round(
            (_parse_actual(final_ata, "ATA") - _parse_actual(final_atd, "ATD"))
            .total_seconds() / 3600.0, 2)
    return {
        "flight_id": flight_id,
        "flight_number": flight.get("flight_number"),
        "atd": final_atd,
        "ata": final_ata,
        "actual_block_hours": actual_block,
        "edited": editing,
    }
