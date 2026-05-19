"""Aircraft CRUD — backend-of-record for the fleet.

Until Sprint 3 the fleet page wrote to a local SQLite cache only and
the Supabase `aircraft` table sat empty. That worked while flights
referenced aircraft by registration string, but it breaks the moment
something (defects, MEL, maintenance checks) carries a real foreign key
to aircraft(id). This endpoint promotes the Supabase row to source of
truth — the Flutter fleet page now reads/writes through here, and the
local SQLite cache becomes a pure offline mirror.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/aircraft", tags=["Aircraft"])
log = logging.getLogger(__name__)

# Same RBAC pattern as the rest of operational data: management writes,
# operational staff read.
_EDITORS = {"super_admin", "admin", "ops_manager"}
_READERS = _EDITORS | {
    "scheduler", "crew_allocator", "cabin_allocator", "cockpit_allocator",
    "ground_allocator", "compliance_officer", "flight_movement",
    "flight_ops", "flight_operations",
}

_VALID_STATUS = {"active", "maintenance", "aog", "grounded"}


def _ensure_reader(u: dict) -> None:
    if u.get("role") not in _READERS:
        raise ForbiddenError("Only operations staff can browse the fleet")


def _ensure_editor(u: dict) -> None:
    if u.get("role") not in _EDITORS:
        raise ForbiddenError("Only admin / ops manager can edit the fleet")


@router.get("")
async def list_aircraft(current_user: CurrentUser, sb: SbClient):
    _ensure_reader(current_user)
    res = sb.table("aircraft").select("*") \
        .eq("company_id", current_user["company_id"]) \
        .order("registration").execute()
    return res.data or []


@router.post("", status_code=201)
async def create_aircraft(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)
    reg = (data.get("registration") or "").strip().upper()
    a_type = (data.get("type") or data.get("aircraft_type") or "").strip()
    if not reg or not a_type:
        raise HTTPException(status_code=422, detail="registration and type are required")

    # Unique registration per company
    existing = sb.table("aircraft").select("id") \
        .eq("company_id", current_user["company_id"]) \
        .eq("registration", reg).execute()
    if existing.data:
        raise HTTPException(status_code=409,
                            detail=f"Aircraft '{reg}' already exists")

    op_status = (data.get("operational_status") or "active").lower()
    if op_status not in _VALID_STATUS:
        raise HTTPException(status_code=422,
            detail=f"operational_status must be one of {sorted(_VALID_STATUS)}")

    row = {
        "id":                  str(uuid.uuid4()),
        "company_id":          current_user["company_id"],
        "aircraft_type":       a_type,
        "registration":        reg,
        "name":                data.get("name"),
        "manufacturer":        data.get("manufacturer"),
        "min_crew":            int(data.get("min_crew", 2)),
        "max_crew":            int(data.get("max_crew", 10)),
        "capacity":            data.get("capacity"),
        "is_active":           op_status == "active",
    }
    # operational_status / status_reason / status_changed_at columns may or
    # may not exist depending on which migrations have been applied.
    # Try-insert with them; fall back to legacy if PostgREST complains.
    extended = {**row,
        "operational_status": op_status,
        "status_reason":      data.get("status_reason"),
        "status_changed_at":  datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = sb.table("aircraft").insert(extended).execute()
    except Exception as e:
        log.warning("aircraft insert with extended cols failed (%s), falling back", e)
        res = sb.table("aircraft").insert(row).execute()
    return res.data[0] if res.data else row


@router.patch("/{aircraft_id}")
async def update_aircraft(aircraft_id: str, data: dict,
                           current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)
    existing = sb.table("aircraft").select("id") \
        .eq("id", aircraft_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Aircraft", aircraft_id)

    allowed = {
        "aircraft_type", "registration", "name", "manufacturer",
        "min_crew", "max_crew", "capacity",
        "is_active", "operational_status", "status_reason",
    }
    update = {k: data[k] for k in data if k in allowed}

    # Normalise registration capitalisation
    if "registration" in update and update["registration"]:
        update["registration"] = update["registration"].strip().upper()

    if "operational_status" in update:
        s = (update["operational_status"] or "active").lower()
        if s not in _VALID_STATUS:
            raise HTTPException(status_code=422,
                detail=f"operational_status must be one of {sorted(_VALID_STATUS)}")
        update["operational_status"]  = s
        update["is_active"]            = s == "active"
        update["status_changed_at"]    = datetime.now(timezone.utc).isoformat()

    try:
        res = sb.table("aircraft").update(update).eq("id", aircraft_id).execute()
    except Exception as e:
        # Fall back without the new columns if the migration hasn't run
        log.warning("aircraft update failed (%s), retrying with legacy cols", e)
        legacy = {k: v for k, v in update.items()
                  if k not in {"operational_status", "status_reason", "status_changed_at"}}
        res = sb.table("aircraft").update(legacy).eq("id", aircraft_id).execute()
    return res.data[0] if res.data else {}


@router.delete("/{aircraft_id}", status_code=204)
async def delete_aircraft(aircraft_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)
    res = sb.table("aircraft").delete() \
        .eq("id", aircraft_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("Aircraft", aircraft_id)
    return None
