"""Crew bidding, lodging catalogue, ground transport.

Three small CRUD modules wired under one router because they all live
in the "crew operations" space.

Bidding rules:
  - Crew can read/write their OWN bid for any unlocked month.
  - Once the scheduler locks the month (typically once the roster is
    published), the bid becomes read-only for the crew.
  - Schedulers can see all bids when building the schedule.

Seniority ordering is computed at read time so we don't have to
maintain a stored rank — newer hires push everyone else down by one.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/crew-ops", tags=["Crew Bidding & Lodging"])
log = logging.getLogger(__name__)

_SCHEDULER_ROLES = {"super_admin", "admin", "ops_manager", "scheduler"}


def _ensure_scheduler(u: dict) -> None:
    if u.get("role") not in _SCHEDULER_ROLES:
        raise ForbiddenError("Only scheduling staff can perform this action")


# ──────────────────────────────────────────────────────────────────────
# Seniority list
# ──────────────────────────────────────────────────────────────────────

@router.get("/seniority")
async def seniority_list(current_user: CurrentUser, sb: SbClient,
                          rank: str | None = Query(None)):
    """Return crew sorted by seniority_date ASC (oldest = #1).

    Anyone in scheduling can see the whole list; crew sees only their
    own row (or rather, the position they hold).
    """
    company_id = current_user["company_id"]
    q = sb.table("crew").select(
        "id,full_name_ar,full_name_en,rank,employee_id,base,seniority_date,hire_date,status"
    ).eq("company_id", company_id).eq("status", "active")
    if rank:
        q = q.eq("rank", rank)
    res = q.execute()
    rows = res.data or []

    # Sort by seniority_date (older = senior); nulls go last
    def _key(r):
        sd = r.get("seniority_date") or r.get("hire_date") or "9999-12-31"
        return sd
    rows.sort(key=_key)
    for i, r in enumerate(rows, start=1):
        r["seniority_rank"] = i

    # Crew sees the full list ordering but only their identifiers
    if current_user.get("role") == "crew":
        own = current_user.get("crew_id")
        return [{"seniority_rank": r["seniority_rank"], "is_me": r["id"] == own,
                 "name": r["full_name_ar"] or r["full_name_en"], "rank": r["rank"]}
                for r in rows]
    return rows


# ──────────────────────────────────────────────────────────────────────
# Crew bids
# ──────────────────────────────────────────────────────────────────────

@router.get("/bids")
async def list_bids(current_user: CurrentUser, sb: SbClient,
                     month: str | None = Query(None)):
    """Schedulers list all bids (filterable by month).
    Crew gets only their own."""
    company_id = current_user["company_id"]
    q = sb.table("crew_bids") \
        .select("*, crew:crew_id(full_name_ar,full_name_en,rank,employee_id)") \
        .eq("company_id", company_id)
    if month:
        q = q.eq("month", month)
    if current_user.get("role") == "crew":
        own = current_user.get("crew_id")
        if not own:
            return []
        q = q.eq("crew_id", own)
    res = q.order("submitted_at", desc=True).execute()
    return res.data or []


@router.post("/bids", status_code=201)
async def upsert_bid(data: dict, current_user: CurrentUser, sb: SbClient):
    """Submit or update a bid. Crew can only edit their own row; once
    the bid is locked, all writes are refused."""
    month = (data.get("month") or "").strip()
    if not month or len(month) != 7 or month[4] != "-":
        raise HTTPException(status_code=422, detail="month must be 'YYYY-MM'")

    # Determine the target crew_id
    role = current_user.get("role")
    if role == "crew":
        target_crew_id = current_user.get("crew_id")
        if not target_crew_id:
            raise ForbiddenError("Your account is not linked to a crew record")
    else:
        # Scheduler can file a bid on behalf of a crew member
        target_crew_id = data.get("crew_id")
        if not target_crew_id:
            raise HTTPException(status_code=422, detail="crew_id required")
        _ensure_scheduler(current_user)

    company_id = current_user["company_id"]

    existing = sb.table("crew_bids").select("id,locked") \
        .eq("crew_id", target_crew_id).eq("month", month).execute()

    if existing.data and existing.data[0].get("locked"):
        raise HTTPException(status_code=409,
            detail="Bid is locked — schedule already published")

    prefs = data.get("preferences") or {}
    if not isinstance(prefs, dict):
        raise HTTPException(status_code=422, detail="preferences must be an object")

    payload = {
        "company_id":  company_id,
        "crew_id":     target_crew_id,
        "month":       month,
        "preferences": prefs,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing.data:
        res = sb.table("crew_bids").update(payload) \
            .eq("id", existing.data[0]["id"]).execute()
    else:
        payload["id"] = str(uuid.uuid4())
        res = sb.table("crew_bids").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.post("/bids/lock/{month}")
async def lock_bids(month: str, current_user: CurrentUser, sb: SbClient):
    """Scheduler locks all bids for the month once the roster is published."""
    _ensure_scheduler(current_user)
    now = datetime.now(timezone.utc).isoformat()
    res = sb.table("crew_bids").update({
        "locked": True, "locked_at": now, "updated_at": now,
    }).eq("company_id", current_user["company_id"]).eq("month", month).execute()
    return {"locked": len(res.data or [])}


# ──────────────────────────────────────────────────────────────────────
# Lodging catalogue
# ──────────────────────────────────────────────────────────────────────

@router.get("/lodging")
async def list_lodging(current_user: CurrentUser, sb: SbClient,
                        station: str | None = Query(None)):
    company_id = current_user["company_id"]
    q = sb.table("crew_lodging").select("*").eq("company_id", company_id) \
        .eq("is_active", True)
    if station:
        q = q.eq("station_code", station.upper())
    return q.order("station_code").order("hotel_name").execute().data or []


@router.post("/lodging", status_code=201)
async def create_lodging(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_scheduler(current_user)
    if not (data.get("hotel_name") or "").strip():
        raise HTTPException(status_code=422, detail="hotel_name required")
    if not (data.get("station_code") or "").strip():
        raise HTTPException(status_code=422, detail="station_code required")

    payload = {
        "id":            str(uuid.uuid4()),
        "company_id":    current_user["company_id"],
        "station_code":  data["station_code"].strip().upper(),
        "hotel_name":    data["hotel_name"].strip(),
        "hotel_address": data.get("hotel_address"),
        "phone":         data.get("phone"),
        "distance_min":  data.get("distance_min"),
        "rating":        data.get("rating"),
        "notes":         data.get("notes"),
        "is_default":    bool(data.get("is_default", False)),
    }
    res = sb.table("crew_lodging").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.delete("/lodging/{lodging_id}", status_code=204)
async def delete_lodging(lodging_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_scheduler(current_user)
    sb.table("crew_lodging").delete() \
        .eq("id", lodging_id) \
        .eq("company_id", current_user["company_id"]).execute()
    return None


@router.post("/lodging/assignments", status_code=201)
async def assign_lodging(data: dict, current_user: CurrentUser, sb: SbClient):
    """Book a crew member into a specific hotel for a layover."""
    _ensure_scheduler(current_user)
    required = {"crew_id", "lodging_id", "check_in_at", "check_out_at"}
    missing = required - set(data.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"missing: {sorted(missing)}")

    payload = {
        "id":           str(uuid.uuid4()),
        "company_id":   current_user["company_id"],
        "crew_id":      data["crew_id"],
        "lodging_id":   data["lodging_id"],
        "flight_id":    data.get("flight_id"),
        "check_in_at":  data["check_in_at"],
        "check_out_at": data["check_out_at"],
        "room_number":  data.get("room_number"),
        "notes":        data.get("notes"),
        "created_by":   current_user["id"],
    }
    res = sb.table("crew_lodging_assignment").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.get("/lodging/assignments")
async def list_lodging_assignments(current_user: CurrentUser, sb: SbClient,
                                     crew_id: str | None = None,
                                     flight_id: str | None = None):
    """Schedulers see all; crew see their own only."""
    q = sb.table("crew_lodging_assignment").select(
        "*, crew:crew_id(full_name_ar,full_name_en), "
        "lodging:lodging_id(hotel_name,station_code,phone)"
    ).eq("company_id", current_user["company_id"])

    if current_user.get("role") == "crew":
        own = current_user.get("crew_id")
        if not own: return []
        q = q.eq("crew_id", own)
    elif crew_id:
        q = q.eq("crew_id", crew_id)

    if flight_id:
        q = q.eq("flight_id", flight_id)

    return q.order("check_in_at", desc=True).execute().data or []


# ──────────────────────────────────────────────────────────────────────
# Ground transport bookings
# ──────────────────────────────────────────────────────────────────────

@router.get("/transport")
async def list_transport(current_user: CurrentUser, sb: SbClient,
                          crew_id: str | None = None,
                          flight_id: str | None = None):
    q = sb.table("crew_transport").select(
        "*, crew:crew_id(full_name_ar,full_name_en)"
    ).eq("company_id", current_user["company_id"])

    if current_user.get("role") == "crew":
        own = current_user.get("crew_id")
        if not own: return []
        q = q.eq("crew_id", own)
    elif crew_id:
        q = q.eq("crew_id", crew_id)

    if flight_id:
        q = q.eq("flight_id", flight_id)

    return q.order("pickup_at", desc=True).execute().data or []


@router.post("/transport", status_code=201)
async def create_transport(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_scheduler(current_user)
    required = {"crew_id", "direction", "pickup_at",
                "pickup_location", "dropoff_location"}
    missing = required - set(data.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"missing: {sorted(missing)}")

    direction = data["direction"].lower()
    if direction not in {"pickup", "dropoff"}:
        raise HTTPException(status_code=422,
            detail="direction must be 'pickup' or 'dropoff'")

    payload = {
        "id":              str(uuid.uuid4()),
        "company_id":      current_user["company_id"],
        "crew_id":         data["crew_id"],
        "flight_id":       data.get("flight_id"),
        "direction":       direction,
        "pickup_at":       data["pickup_at"],
        "pickup_location": data["pickup_location"],
        "dropoff_location": data["dropoff_location"],
        "vehicle_plate":   data.get("vehicle_plate"),
        "driver_name":     data.get("driver_name"),
        "driver_phone":    data.get("driver_phone"),
        "notes":           data.get("notes"),
        "status":          "planned",
        "created_by":      current_user["id"],
    }
    res = sb.table("crew_transport").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.patch("/transport/{id}")
async def update_transport(id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_scheduler(current_user)
    allowed = {"status", "vehicle_plate", "driver_name", "driver_phone",
               "pickup_at", "pickup_location", "dropoff_location", "notes"}
    update = {k: v for k, v in data.items() if k in allowed}
    res = sb.table("crew_transport").update(update).eq("id", id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("Transport booking", id)
    return res.data[0]
