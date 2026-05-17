import uuid, os, math, logging
from datetime import date, timedelta, datetime, timezone
from fastapi import APIRouter, Query
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ForbiddenError
from app.core.config import settings

router = APIRouter(prefix="/documents", tags=["Documents"])
log = logging.getLogger(__name__)


def _verify_doc_in_company(sb, doc_id: str, company_id: str) -> dict | None:
    """Fetch the document only if its crew belongs to the caller's company.
    Returns the document row (with crew_id) or None. Uses a single inner-joined
    query so it cannot be bypassed via TOCTOU on a separate verification call."""
    res = sb.table("documents") \
        .select("id, crew_id, crew!inner(company_id)") \
        .eq("id", doc_id) \
        .eq("crew.company_id", company_id) \
        .execute()
    return res.data[0] if res.data else None


@router.get("/crew/{crew_id}")
async def get_crew_documents(crew_id: str, current_user: CurrentUser, sb: SbClient):
    crew_check = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not crew_check.data:
        raise NotFoundError("Crew member", crew_id)
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
    crew_id_in = data.get("crew_id")
    if not crew_id_in:
        raise NotFoundError("Crew member", "missing")
    crew = sb.table("crew").select("id").eq("id", crew_id_in).eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Crew member", crew_id_in)

    data["id"] = str(uuid.uuid4())
    data["is_verified"] = False
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("documents").insert(data).execute()
    return result.data[0] if result.data else {}


@router.patch("/{doc_id}")
async def update_document(doc_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Update a document (e.g. expiry date, doc number)."""
    if not _verify_doc_in_company(sb, doc_id, current_user["company_id"]):
        raise NotFoundError("Document", doc_id)
    # Strip caller-controlled fields that must not be patched
    for forbidden in ("id", "crew_id", "company_id", "created_at"):
        data.pop(forbidden, None)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("documents").update(data).eq("id", doc_id).execute()
    return result.data[0] if result.data else {}


@router.delete("/{doc_id}", status_code=204)
async def delete_document(doc_id: str, current_user: CurrentUser, sb: SbClient):
    """Delete a document. Admin / Ops Manager only."""
    if current_user["role"] not in ("super_admin", "admin", "ops_manager"):
        raise ForbiddenError("Insufficient permissions")

    doc = _verify_doc_in_company(sb, doc_id, current_user["company_id"])
    if not doc:
        raise NotFoundError("Document", doc_id)

    sb.table("documents").delete().eq("id", doc_id).execute()

    try:
        sb.table("audit_log").insert({
            "user_id":     current_user["id"],
            "user_name":   current_user.get("name_ar") or current_user.get("name_en") or current_user["email"],
            "action":      "delete_document",
            "entity_type": "document",
            "entity_id":   doc_id,
            "company_id":  current_user["company_id"],
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        log.exception("Failed to write audit log for document delete")


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
