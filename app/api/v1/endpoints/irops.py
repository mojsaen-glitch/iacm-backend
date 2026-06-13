"""IROPS — Irregular Operations management.

Three operations:
  1. Bulk-cancel: cancel many flights atomically with a shared reason.
  2. Cascade impact: list every assignment / downstream flight that's
     affected when a flight cancels (so the dispatcher knows what to
     reassign).
  3. Recovery options: suggest replacement aircraft (active + same type)
     and crew (qualified + not already assigned) for a cancelled flight.

The endpoints are read-heavy by design — we surface options, the
dispatcher makes the final call. No auto-rebooking; that needs human
judgement for an airline this small.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError

router = APIRouter(prefix="/irops", tags=["IROPS"])
log = logging.getLogger(__name__)

# Same gate as flight editors — schedulers + ops can act on disruptions.
_IROPS_EDITORS = {"super_admin", "admin", "ops_manager", "scheduler", "flight_movement"}


def _ensure_irops(user: dict) -> None:
    if user.get("role") not in _IROPS_EDITORS:
        raise ForbiddenError("Only ops / scheduling can manage IROPS")


# ──────────────────────────────────────────────────────────────────────
# Bulk cancellation
# ──────────────────────────────────────────────────────────────────────

@router.post("/bulk-cancel")
async def bulk_cancel(data: dict, current_user: CurrentUser, sb: SbClient):
    """Cancel a list of flights in one operation, all with the same reason.

    Body:
      { flight_ids: [...], reason: 'weather', reason_notes: '...',
        irops_event_id?: 'uuid' }

    Each flight is updated individually so a single failure doesn't
    roll back the whole batch — the response reports per-flight status.
    Also stamps the cancellation onto the optional irops_events row.
    """
    _ensure_irops(current_user)

    flight_ids = data.get("flight_ids") or []
    if not isinstance(flight_ids, list) or not flight_ids:
        raise HTTPException(status_code=422, detail="flight_ids[] is required")
    reason = (data.get("reason") or "").strip().lower()
    notes  = (data.get("reason_notes") or "").strip()
    event_id = data.get("irops_event_id")

    company_id = current_user["company_id"]
    now_iso = datetime.now(timezone.utc).isoformat()
    cancelled, skipped = [], []

    for fid in flight_ids:
        try:
            res = sb.table("flights").update({
                "status":              "cancelled",
                "cancellation_reason": reason or None,
                "cancellation_notes":  notes  or None,
                "updated_at":          now_iso,
            }).eq("id", fid).eq("company_id", company_id).execute()
            if res.data:
                cancelled.append(fid)
            else:
                skipped.append({"flight_id": fid, "error": "not_found"})
        except Exception as e:
            log.exception("bulk_cancel failed for %s", fid)
            skipped.append({"flight_id": fid, "error": str(e)[:120]})

    # Update the IROPS event counter if one was attached
    if event_id and cancelled:
        try:
            existing = sb.table("irops_events").select("flights_cancelled") \
                .eq("id", event_id).execute()
            if existing.data:
                current_cnt = existing.data[0].get("flights_cancelled") or 0
                sb.table("irops_events").update({
                    "flights_cancelled": current_cnt + len(cancelled),
                    "updated_at":        now_iso,
                }).eq("id", event_id).execute()
        except Exception:
            log.exception("Failed to update IROPS event counter")

    return {
        "cancelled_count": len(cancelled),
        "cancelled":       cancelled,
        "skipped":         skipped,
    }


# ──────────────────────────────────────────────────────────────────────
# Cascade impact — what else does this flight cancellation touch?
# ──────────────────────────────────────────────────────────────────────

@router.get("/flights/{flight_id}/cascade-impact")
async def cascade_impact(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Return everything tied to this flight that will be impacted by
    a cancellation:
      • Assigned crew (so they can be notified + rebooked)
      • Same-day return flight by the same crew (if they were
        scheduled to fly back on a turnaround)
    """
    _ensure_irops(current_user)
    company_id = current_user["company_id"]

    flight = sb.table("flights").select(
        "id,flight_number,departure_time,arrival_time,origin_code,destination_code,aircraft_registration"
    ).eq("id", flight_id).eq("company_id", company_id).execute()
    if not flight.data:
        raise HTTPException(status_code=404, detail="Flight not found")
    f = flight.data[0]

    # Crew assigned to this flight
    assignments = sb.table("assignments").select(
        "id, crew_id, role, crew:crew_id(full_name_ar,full_name_en,rank,employee_id)"
    ).eq("flight_id", flight_id).execute()
    crew_rows = assignments.data or []

    # Same-day downstream flights for the same crew (turnaround risk)
    downstream = []
    if crew_rows:
        try:
            arr_dt = datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00"))
            window_end = (arr_dt + timedelta(hours=24)).isoformat()
            for c in crew_rows:
                ds = sb.table("assignments").select(
                    "id,flight:flight_id(id,flight_number,departure_time,origin_code,destination_code)"
                ).eq("crew_id", c["crew_id"]) \
                  .neq("flight_id", flight_id) \
                  .execute()
                for d in (ds.data or []):
                    flt = d.get("flight") or {}
                    dep = flt.get("departure_time")
                    if not dep:
                        continue
                    try:
                        dep_dt = datetime.fromisoformat(dep.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if arr_dt <= dep_dt <= datetime.fromisoformat(window_end.replace("Z", "+00:00")):
                        downstream.append({
                            "crew_id": c["crew_id"],
                            "flight":  flt,
                        })
        except Exception:
            log.exception("Failed building downstream cascade")

    return {
        "flight":     f,
        "crew_count": len(crew_rows),
        "crew":       crew_rows,
        "downstream_flights": downstream,
    }


# ──────────────────────────────────────────────────────────────────────
# Recovery options — what can I substitute?
# ──────────────────────────────────────────────────────────────────────

@router.get("/flights/{flight_id}/recovery-options")
async def recovery_options(flight_id: str, current_user: CurrentUser, sb: SbClient):
    """Suggest replacement aircraft + standby crew for a disrupted flight.

    Aircraft: same type, status=active, not already busy in the flight window.
    Crew:    same rank, status=active, no overlapping assignment.

    Returns the suggestions ranked by simple heuristic (recently flown
    not preferred — fairness in IROPS comes for free).
    """
    _ensure_irops(current_user)
    company_id = current_user["company_id"]

    flight = sb.table("flights").select(
        "id,aircraft_type,aircraft_registration,departure_time,arrival_time,"
        "origin_code,destination_code"
    ).eq("id", flight_id).eq("company_id", company_id).execute()
    if not flight.data:
        raise HTTPException(status_code=404, detail="Flight not found")
    f = flight.data[0]

    # Replacement aircraft — same type, active, not the current tail
    aircraft_res = sb.table("aircraft").select("*") \
        .eq("company_id", company_id) \
        .eq("aircraft_type", f.get("aircraft_type") or "") \
        .eq("operational_status", "active") \
        .neq("registration", f.get("aircraft_registration") or "")
    aircraft_rows = aircraft_res.execute().data or []

    # Filter: skip tails that already have an overlapping flight
    try:
        dep = datetime.fromisoformat(f["departure_time"].replace("Z", "+00:00"))
        arr = datetime.fromisoformat(f["arrival_time"].replace("Z", "+00:00"))
        busy_q = sb.table("flights").select("aircraft_registration") \
            .eq("company_id", company_id) \
            .gte("departure_time", (dep - timedelta(hours=2)).isoformat()) \
            .lte("departure_time", (arr + timedelta(hours=2)).isoformat()) \
            .neq("status", "cancelled") \
            .execute()
        busy_regs = {r.get("aircraft_registration") for r in (busy_q.data or [])
                     if r.get("aircraft_registration")}
        aircraft_rows = [a for a in aircraft_rows
                         if a.get("registration") not in busy_regs]
    except Exception:
        log.exception("Aircraft availability filter failed")

    # Replacement crew — active, no overlapping assignment.
    crew_res = sb.table("crew").select(
        "id,full_name_ar,full_name_en,rank,base,employee_id,monthly_flight_hours,total_flight_hours"
    ).eq("company_id", company_id).eq("status", "active").execute()
    crew_rows = crew_res.data or []

    # Already-assigned crew IDs in the window
    assigned_ids = set()
    try:
        win_start = (dep - timedelta(hours=2)).isoformat()
        win_end   = (arr + timedelta(hours=2)).isoformat()
        a_q = sb.table("assignments").select(
            "crew_id, flight:flight_id(departure_time,status)"
        ).execute()
        for row in (a_q.data or []):
            fl = row.get("flight") or {}
            if (fl.get("status") or "") == "cancelled":
                continue
            t = fl.get("departure_time")
            if t and win_start <= t <= win_end:
                assigned_ids.add(row.get("crew_id"))
    except Exception:
        log.exception("Crew availability filter failed")

    available_crew = [c for c in crew_rows if c["id"] not in assigned_ids]

    # Fairness rank: lowest REAL credited month hours first (one batch join —
    # same engine policy as the Monthly Hours matrix, cancelled excluded). The
    # stored crew.monthly_flight_hours is NOT maintained (usually 0 for
    # everyone) and stays only as a fallback if the batch computation fails.
    real_hours: dict = {}
    try:
        from app.core.monthly_hours import month_hours_by_crew
        real_hours = month_hours_by_crew(sb, company_id)
    except Exception:
        log.exception("month-hours batch failed — ranking falls back to stored field")
    for c in available_crew:
        c["computed_month_hours"] = (
            real_hours.get(c["id"], 0.0) if real_hours
            else float(c.get("monthly_flight_hours") or 0))
    available_crew.sort(key=lambda c: (
        float(c.get("computed_month_hours") or 0),
        float(c.get("total_flight_hours")   or 0),
    ))

    # R5 — UNIFY with the managed standby pool: read reserves FIRST, ranked by
    # the SAME standby suggest logic (compliant incl. FDP first). The general
    # active-crew list below stays as a fallback (when no valid reserve exists).
    # This view never assigns — acceptance + assignment stay in R2 → /assignments.
    standby_options = []
    try:
        from app.api.v1.endpoints.standby import _rank_standby_candidates
        standby_options = _rank_standby_candidates(sb, company_id, f)
    except Exception:
        log.exception("standby pool ranking failed for recovery-options")
    has_valid_standby = any(
        c.get("compliance_status") not in ("BLOCKED", "RED") for c in standby_options)

    return {
        "flight": f,
        "aircraft_options": aircraft_rows[:10],
        # Managed reserves first (R5); general active crew is the fallback.
        "standby_options":   standby_options[:20],
        "has_valid_standby": has_valid_standby,
        "crew_options":      available_crew[:20],
    }


# ──────────────────────────────────────────────────────────────────────
# IROPS events CRUD — for tracking disruptions
# ──────────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(current_user: CurrentUser, sb: SbClient,
                       active_only: bool = False):
    _ensure_irops(current_user)
    q = sb.table("irops_events").select("*") \
        .eq("company_id", current_user["company_id"])
    if active_only:
        q = q.is_("cleared_at", "null")
    res = q.order("started_at", desc=True).execute()
    return res.data or []


@router.post("/events", status_code=201)
async def create_event(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_irops(current_user)
    if not (data.get("title") or "").strip():
        raise HTTPException(status_code=422, detail="title required")

    payload = {
        "id":               str(uuid.uuid4()),
        "company_id":       current_user["company_id"],
        "event_type":       (data.get("event_type") or "other").lower(),
        "title":            data["title"].strip(),
        "description":      data.get("description"),
        "affected_station": data.get("affected_station"),
        "severity":         (data.get("severity") or "major").lower(),
        "expected_clear_at": data.get("expected_clear_at"),
        "started_at":       datetime.now(timezone.utc).isoformat(),
        "created_by":       current_user["id"],
    }
    res = sb.table("irops_events").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.post("/events/{event_id}/close")
async def close_event(event_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_irops(current_user)
    now = datetime.now(timezone.utc).isoformat()
    res = sb.table("irops_events").update({
        "cleared_at": now,
        "closed_by":  current_user["id"],
        "updated_at": now,
    }).eq("id", event_id) \
      .eq("company_id", current_user["company_id"]) \
      .execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="event not found")
    return res.data[0]
