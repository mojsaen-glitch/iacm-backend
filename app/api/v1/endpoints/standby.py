"""Standby / Reserve crew management.

Schedulers put crew on call (Airport / Home / Ready / Long-call) for a window;
when a flight is short-crewed, Operations can call them out. The suggest
endpoint ranks eligible standby crew for a flight using the ComplianceEngine
(so a blocked/over-FDP reserve is never offered first).
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import NotFoundError, ForbiddenError
from app.core.compliance_engine import ComplianceEngine, IRAQI_AIRPORTS

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

    flight_id = data.get("flight_id")
    update = {
        "called_out": True,
        "status":     "ASSIGNED" if flight_id else "CALLED_OUT",
        "assigned_flight_id": flight_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    res = sb.table("standby_assignments").update(update).eq("id", standby_id).execute()
    return res.data[0] if res.data else {}


@router.post("/{standby_id}/cancel")
async def cancel_standby(standby_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_manager(current_user)
    existing = sb.table("standby_assignments").select("id").eq("id", standby_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Standby", standby_id)
    res = sb.table("standby_assignments").update({
        "status": "CANCELLED",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", standby_id).execute()
    return res.data[0] if res.data else {}


@router.delete("/{standby_id}", status_code=204)
async def delete_standby(standby_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_manager(current_user)
    existing = sb.table("standby_assignments").select("id").eq("id", standby_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Standby", standby_id)
    sb.table("standby_assignments").delete().eq("id", standby_id).execute()


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

    def _dt(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None
    dep = _dt(flight.get("departure_time"))
    arr = _dt(flight.get("arrival_time"))
    intl = (flight.get("origin_code", "").upper() not in IRAQI_AIRPORTS or
            flight.get("destination_code", "").upper() not in IRAQI_AIRPORTS)

    rows = sb.table("standby_assignments").select("*") \
        .eq("company_id", current_user["company_id"]).eq("status", "ACTIVE") \
        .execute().data or []
    # Keep only reserves whose window covers the departure.
    candidates = []
    for r in rows:
        s, e = _dt(r.get("start_time")), _dt(r.get("end_time"))
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
