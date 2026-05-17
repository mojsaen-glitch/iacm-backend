import uuid, math
from datetime import datetime, timezone, date, timedelta
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ForbiddenError

router = APIRouter(prefix="/training", tags=["Training Records"])

MANAGER_ROLES = {"super_admin", "admin", "ops_manager", "compliance_officer"}


@router.get("/crew/{crew_id}")
async def get_crew_training(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """All training records for a crew member."""
    crew_check = sb.table("crew").select("id").eq("id", crew_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)

    result = sb.table("training_records").select("*").eq("crew_id", crew_id)\
        .order("expiry_date", desc=False).execute()

    today = date.today()
    warn  = today + timedelta(days=30)
    records = []
    for r in (result.data or []):
        status = "valid"
        if r.get("expiry_date"):
            exp = date.fromisoformat(r["expiry_date"][:10])
            if exp < today:
                status = "expired"
            elif exp <= warn:
                status = "expiring"
        r["status"] = status
        records.append(r)
    return records


@router.get("/expiring")
async def get_expiring_training(
    current_user: CurrentUser,
    sb: SbClient,
    days: int = Query(30, ge=1, le=90),
):
    """Training records expiring within N days for the whole company."""
    today   = date.today().isoformat()
    warning = (date.today() + timedelta(days=days)).isoformat()
    result  = sb.table("training_records")\
        .select("*, crew!inner(company_id, full_name_ar, full_name_en, rank, employee_id)")\
        .eq("crew.company_id", current_user["company_id"])\
        .lte("expiry_date", warning).gte("expiry_date", today)\
        .order("expiry_date").execute()
    return result.data or []


@router.post("", status_code=201)
async def create_training(data: dict, current_user: CurrentUser, sb: SbClient):
    """Add a training record. Managers and compliance officers only."""
    if current_user["role"] not in MANAGER_ROLES:
        raise ForbiddenError("يتطلب صلاحية مدير أو ضابط امتثال")

    crew_id = data.get("crew_id", "").strip()
    if not crew_id:
        raise HTTPException(status_code=422, detail="crew_id مطلوب")

    crew_check = sb.table("crew").select("id").eq("id", crew_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)

    if not data.get("training_type"):
        raise HTTPException(status_code=422, detail="training_type مطلوب")
    if not data.get("completion_date"):
        raise HTTPException(status_code=422, detail="completion_date مطلوب")

    record = {
        "id":                 str(uuid.uuid4()),
        "crew_id":            crew_id,
        "company_id":         current_user["company_id"],
        "training_type":      data["training_type"],
        "aircraft_type":      data.get("aircraft_type"),
        "completion_date":    data["completion_date"],
        "expiry_date":        data.get("expiry_date"),
        "trainer":            data.get("trainer", ""),
        "certificate_number": data.get("certificate_number", ""),
        "notes":              data.get("notes", ""),
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }

    result = sb.table("training_records").insert(record).execute()
    return result.data[0] if result.data else record


@router.patch("/{record_id}")
async def update_training(record_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Update a training record (e.g. extend expiry). Managers only."""
    if current_user["role"] not in MANAGER_ROLES:
        raise ForbiddenError("يتطلب صلاحية مدير أو ضابط امتثال")

    existing = sb.table("training_records").select("id,company_id")\
        .eq("id", record_id).execute()
    if not existing.data:
        raise NotFoundError("Training record", record_id)
    if existing.data[0]["company_id"] != current_user["company_id"]:
        raise ForbiddenError("لا يمكنك تعديل سجل تدريب خارج شركتك")

    data.pop("id", None)
    data.pop("crew_id", None)
    data.pop("company_id", None)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("training_records").update(data).eq("id", record_id).execute()
    return result.data[0] if result.data else {}


@router.delete("/{record_id}", status_code=204)
async def delete_training(record_id: str, current_user: CurrentUser, sb: SbClient):
    """Delete a training record. Admin only."""
    if current_user["role"] not in {"super_admin", "admin"}:
        raise ForbiddenError("Admin access required")

    existing = sb.table("training_records").select("id,company_id")\
        .eq("id", record_id).execute()
    if not existing.data:
        raise NotFoundError("Training record", record_id)
    if existing.data[0]["company_id"] != current_user["company_id"]:
        raise ForbiddenError("لا يمكنك حذف سجل تدريب خارج شركتك")

    sb.table("training_records").delete().eq("id", record_id).execute()
