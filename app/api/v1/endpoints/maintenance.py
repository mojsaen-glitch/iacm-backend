"""Maintenance & Engineering — defects, MEL deferrals, recurring checks.

Three small CRUD endpoints + one aggregate that powers the aircraft
health badge in the fleet list. A defect logged with `grounding=true`
flips the aircraft.operational_status row in the SAME request so
engineering and dispatch never see different states.
"""

import logging
import uuid
from datetime import datetime, timezone, date

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/maintenance", tags=["Maintenance"])
log = logging.getLogger(__name__)

# Engineering writes. Crew/scheduler can READ (defect badges) but not edit.
_ENG_ROLES   = {"super_admin", "admin", "ops_manager"}
_READ_ROLES  = _ENG_ROLES | {
    "scheduler", "crew_allocator", "cabin_allocator", "cockpit_allocator",
    "ground_allocator", "flight_movement", "flight_ops", "compliance_officer",
}


def _ensure_eng(user: dict) -> None:
    if user.get("role") not in _ENG_ROLES:
        raise ForbiddenError("Only engineering/ops can edit maintenance records")


def _ensure_read(user: dict) -> None:
    if user.get("role") not in _READ_ROLES:
        raise ForbiddenError("Maintenance data is restricted to operations staff")


# ──────────────────────────────────────────────────────────────────────
# Defects
# ──────────────────────────────────────────────────────────────────────

@router.get("/defects")
async def list_defects(current_user: CurrentUser, sb: SbClient,
                        aircraft_id: str | None = None,
                        status: str | None = Query(None, description="open | deferred | resolved")):
    _ensure_read(current_user)
    q = sb.table("defects") \
        .select("*, aircraft:aircraft_id(registration,type,operational_status)") \
        .eq("company_id", current_user["company_id"])
    if aircraft_id: q = q.eq("aircraft_id", aircraft_id)
    if status:      q = q.eq("status", status)
    return q.order("reported_at", desc=True).execute().data or []


@router.post("/defects", status_code=201)
async def create_defect(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_eng(current_user)
    if not data.get("aircraft_id") or not data.get("title"):
        raise HTTPException(status_code=422, detail="aircraft_id and title are required")

    sev = (data.get("severity") or "minor").lower()
    if sev not in {"minor", "major", "critical"}:
        raise HTTPException(status_code=422, detail="severity must be minor/major/critical")
    grounding = bool(data.get("grounding")) or sev == "critical"

    payload = {
        "id":          str(uuid.uuid4()),
        "company_id":  current_user["company_id"],
        "aircraft_id": data["aircraft_id"],
        "title":       data["title"],
        "description": data.get("description"),
        "severity":    sev,
        "grounding":   grounding,
        "status":      "open",
        "reported_by": current_user["id"],
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }
    res = sb.table("defects").insert(payload).execute()

    # Auto-AOG if the defect grounds the aircraft. Engineering and
    # dispatch should never disagree on whether a tail can fly.
    if grounding:
        sb.table("aircraft").update({
            "operational_status": "aog",
            "status_reason":      f"AOG: {data['title']}",
            "status_changed_at":  datetime.now(timezone.utc).isoformat(),
            "is_active":          False,
        }).eq("id", data["aircraft_id"]).eq("company_id", current_user["company_id"]).execute()

    return res.data[0] if res.data else payload


@router.patch("/defects/{defect_id}")
async def update_defect(defect_id: str, data: dict,
                         current_user: CurrentUser, sb: SbClient):
    _ensure_eng(current_user)
    existing = sb.table("defects").select("*").eq("id", defect_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Defect", defect_id)

    allowed = {"title", "description", "severity", "grounding",
               "status", "resolution", "mel_item_id"}
    update = {k: v for k, v in data.items() if k in allowed}

    # When transitioning to resolved, stamp the resolver
    if update.get("status") == "resolved":
        update["resolved_by"] = current_user["id"]
        update["resolved_at"] = datetime.now(timezone.utc).isoformat()

    res = sb.table("defects").update(update).eq("id", defect_id).execute()
    row = res.data[0] if res.data else existing.data[0]

    # If this defect was the reason the tail was AOG and we just resolved
    # it, AND no other open grounding defects remain, return tail to active.
    if update.get("status") == "resolved":
        aircraft_id = row["aircraft_id"]
        open_ground = sb.table("defects").select("id", count="exact") \
            .eq("aircraft_id", aircraft_id) \
            .eq("grounding", True) \
            .neq("status", "resolved") \
            .execute()
        if (open_ground.count or 0) == 0:
            sb.table("aircraft").update({
                "operational_status": "active",
                "status_reason":      None,
                "status_changed_at":  datetime.now(timezone.utc).isoformat(),
                "is_active":          True,
            }).eq("id", aircraft_id).eq("company_id", current_user["company_id"]).execute()

    return row


@router.delete("/defects/{defect_id}", status_code=204)
async def delete_defect(defect_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_eng(current_user)
    res = sb.table("defects").delete().eq("id", defect_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("Defect", defect_id)
    return None


# ──────────────────────────────────────────────────────────────────────
# MEL items
# ──────────────────────────────────────────────────────────────────────

@router.get("/mel")
async def list_mel(current_user: CurrentUser, sb: SbClient,
                    aircraft_id: str | None = None,
                    cleared: bool | None = None):
    _ensure_read(current_user)
    q = sb.table("mel_items") \
        .select("*, aircraft:aircraft_id(registration,type)") \
        .eq("company_id", current_user["company_id"])
    if aircraft_id:        q = q.eq("aircraft_id", aircraft_id)
    if cleared is not None: q = q.eq("cleared", cleared)
    return q.order("deadline").execute().data or []


@router.post("/mel", status_code=201)
async def create_mel(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_eng(current_user)
    required = {"aircraft_id", "mel_reference", "description", "deadline"}
    missing = required - set(data.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"missing: {sorted(missing)}")

    payload = {
        "id":            str(uuid.uuid4()),
        "company_id":    current_user["company_id"],
        "aircraft_id":   data["aircraft_id"],
        "mel_reference": data["mel_reference"],
        "description":   data["description"],
        "category":      (data.get("category") or "C").upper(),
        "deferred_at":   datetime.now(timezone.utc).isoformat(),
        "deadline":      data["deadline"],
        "cleared":       False,
        "notes":         data.get("notes"),
    }
    res = sb.table("mel_items").insert(payload).execute()
    return res.data[0] if res.data else payload


@router.post("/mel/{mel_id}/clear")
async def clear_mel(mel_id: str, current_user: CurrentUser, sb: SbClient):
    """Engineer marks a deferred item as fixed."""
    _ensure_eng(current_user)
    res = sb.table("mel_items").update({
        "cleared":    True,
        "cleared_at": datetime.now(timezone.utc).isoformat(),
        "cleared_by": current_user["id"],
    }).eq("id", mel_id).eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("MEL item", mel_id)
    return res.data[0]


# ──────────────────────────────────────────────────────────────────────
# Maintenance checks
# ──────────────────────────────────────────────────────────────────────

@router.get("/checks")
async def list_checks(current_user: CurrentUser, sb: SbClient,
                       aircraft_id: str | None = None):
    _ensure_read(current_user)
    q = sb.table("maintenance_checks") \
        .select("*, aircraft:aircraft_id(registration,type)") \
        .eq("company_id", current_user["company_id"])
    if aircraft_id: q = q.eq("aircraft_id", aircraft_id)
    return q.order("next_due_date").execute().data or []


@router.post("/checks", status_code=201)
async def upsert_check(data: dict, current_user: CurrentUser, sb: SbClient):
    """Save or update a recurring check for an aircraft.

    Upserts by (aircraft_id, check_type) so re-saving the same A-check
    doesn't create dupes.
    """
    _ensure_eng(current_user)
    required = {"aircraft_id", "check_type"}
    missing = required - set(data.keys())
    if missing:
        raise HTTPException(status_code=422, detail=f"missing: {sorted(missing)}")

    company_id = current_user["company_id"]
    existing = sb.table("maintenance_checks").select("id") \
        .eq("aircraft_id", data["aircraft_id"]) \
        .eq("check_type",  data["check_type"]) \
        .eq("company_id",  company_id) \
        .execute()

    payload = {
        "company_id":      company_id,
        "aircraft_id":     data["aircraft_id"],
        "check_type":      data["check_type"],
        "last_done":       data.get("last_done"),
        "last_done_hours": data.get("last_done_hours"),
        "next_due_date":   data.get("next_due_date"),
        "next_due_hours":  data.get("next_due_hours"),
        "interval_days":   data.get("interval_days"),
        "interval_hours":  data.get("interval_hours"),
        "notes":           data.get("notes"),
    }
    if existing.data:
        res = sb.table("maintenance_checks").update(payload) \
            .eq("id", existing.data[0]["id"]).execute()
    else:
        payload["id"] = str(uuid.uuid4())
        res = sb.table("maintenance_checks").insert(payload).execute()
    return res.data[0] if res.data else payload


# ──────────────────────────────────────────────────────────────────────
# Aircraft health summary — for the fleet badge
# ──────────────────────────────────────────────────────────────────────

@router.get("/aircraft/{aircraft_id}/health")
async def aircraft_health(aircraft_id: str,
                           current_user: CurrentUser, sb: SbClient):
    """Single endpoint the fleet UI calls per aircraft to render its
    health badge. Returns counts of open defects, deferred MEL items,
    and the soonest upcoming check.
    """
    _ensure_read(current_user)
    company_id = current_user["company_id"]

    open_defects = sb.table("defects").select("id", count="exact") \
        .eq("aircraft_id", aircraft_id) \
        .eq("company_id", company_id) \
        .neq("status", "resolved").execute()

    critical_open = sb.table("defects").select("id", count="exact") \
        .eq("aircraft_id", aircraft_id) \
        .eq("company_id", company_id) \
        .eq("severity", "critical") \
        .neq("status", "resolved").execute()

    open_mel = sb.table("mel_items").select("id,deadline", count="exact") \
        .eq("aircraft_id", aircraft_id) \
        .eq("company_id", company_id) \
        .eq("cleared", False).execute()

    next_check = sb.table("maintenance_checks") \
        .select("check_type,next_due_date") \
        .eq("aircraft_id", aircraft_id) \
        .eq("company_id", company_id) \
        .not_.is_("next_due_date", "null") \
        .order("next_due_date").limit(1).execute()

    # Earliest MEL deadline
    earliest_mel_deadline = None
    if open_mel.data:
        try:
            earliest_mel_deadline = min(
                m["deadline"] for m in open_mel.data
                if m.get("deadline")
            )
        except (ValueError, TypeError):
            pass

    return {
        "open_defects":          open_defects.count or 0,
        "critical_defects":      critical_open.count or 0,
        "open_mel_items":        open_mel.count or 0,
        "earliest_mel_deadline": earliest_mel_deadline,
        "next_check": next_check.data[0] if next_check.data else None,
    }
