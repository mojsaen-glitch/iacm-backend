import uuid, os, math
from datetime import date, timedelta, datetime, timezone
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError
from app.core.config import settings

router = APIRouter(prefix="/documents", tags=["Documents"])


@router.get("/crew/{crew_id}")
async def get_crew_documents(crew_id: str, current_user: CurrentUser, sb: SbClient):
    result = sb.table("documents").select("*").eq("crew_id", crew_id).execute()
    today = date.today()
    warning = today + timedelta(days=30)
    docs = []
    for doc in (result.data or []):
        status = "valid"
        if doc.get("expiry_date"):
            exp = date.fromisoformat(doc["expiry_date"])
            if exp < today:
                status = "expired"
            elif exp <= warning:
                status = "expiring"
        doc["status"] = status
        docs.append(doc)
    return docs


@router.post("", status_code=201)
async def create_document(data: dict, current_user: CurrentUser, sb: SbClient):
    crew = sb.table("crew").select("id").eq("id", data.get("crew_id")).eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Crew member", data.get("crew_id"))

    data["id"] = str(uuid.uuid4())
    data["is_verified"] = False
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("documents").insert(data).execute()
    return result.data[0] if result.data else {}


@router.patch("/{doc_id}")
async def update_document(doc_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Update a document (e.g. expiry date, doc number)."""
    existing = sb.table("documents").select("id,crew_id").eq("id", doc_id).execute()
    if not existing.data:
        raise NotFoundError("Document", doc_id)
    # Verify crew belongs to same company
    crew = sb.table("crew").select("id").eq("id", existing.data[0]["crew_id"])\
        .eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Document", doc_id)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("documents").update(data).eq("id", doc_id).execute()
    return result.data[0] if result.data else {}


@router.delete("/{doc_id}", status_code=204)
async def delete_document(doc_id: str, current_user: CurrentUser, sb: SbClient):
    """Delete a document. Admin / Ops Manager only."""
    if current_user["role"] not in ("super_admin", "admin", "ops_manager"):
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("Insufficient permissions")
    existing = sb.table("documents").select("id,crew_id").eq("id", doc_id).execute()
    if not existing.data:
        raise NotFoundError("Document", doc_id)
    crew = sb.table("crew").select("id").eq("id", existing.data[0]["crew_id"])\
        .eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Document", doc_id)
    sb.table("documents").delete().eq("id", doc_id).execute()


@router.get("/expiring")
async def get_expiring_documents(
    current_user: CurrentUser,
    sb: SbClient,
    days: int = Query(30, ge=1, le=90),
):
    today = date.today().isoformat()
    warning = (date.today() + timedelta(days=days)).isoformat()
    result = sb.table("documents").select("*, crew!inner(company_id, full_name_ar, full_name_en)")\
        .eq("crew.company_id", current_user["company_id"])\
        .lte("expiry_date", warning).gte("expiry_date", today)\
        .order("expiry_date").execute()
    return result.data or []
