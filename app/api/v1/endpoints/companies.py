"""Companies (airlines / operators) registry + management.

The `companies` table doubles as the airline registry: each row is an operator a
crew member can belong to (`crew.operator_company_id`). Listing is open to any
authenticated user (needed for dropdowns + showing the airline next to a crew
member); create / edit / activate-deactivate is limited to super_admin / admin.
There is NO delete — a company linked to crew/assignments is deactivated, not removed.
"""
import uuid
import logging
from datetime import datetime, timezone
from fastapi import APIRouter

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError, ConflictError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["Companies"])

_MANAGERS = {"super_admin", "admin"}


def _ensure_manager(user: dict) -> None:
    if user.get("role") not in _MANAGERS:
        raise ForbiddenError("إدارة الشركات متاحة لـ super_admin / admin فقط")


@router.get("")
async def list_companies(
    current_user: CurrentUser, sb: SbClient,
    active_only: bool = False,
    with_counts: bool = False,
):
    """All companies (airlines). `active_only` for dropdowns; `with_counts` adds the
    number of crew attached to each (manager view)."""
    q = sb.table("companies").select("*")
    if active_only:
        q = q.eq("is_active", True)
    items = (q.order("name_en").execute().data) or []
    if with_counts:
        for c in items:
            try:
                c["crew_count"] = sb.table("crew").select("id", count="exact") \
                    .eq("operator_company_id", c["id"]).limit(1).execute().count or 0
            except Exception as e:
                log.info("crew count failed for company %s: %s", c.get("id"), e)
                c["crew_count"] = 0
    return {"items": items}


@router.post("", status_code=201)
async def create_company(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_manager(current_user)
    name_en = (data.get("name_en") or data.get("name") or "").strip()
    code = (data.get("code") or "").strip()
    if not name_en or not code:
        raise ValidationError("اسم الشركة والكود مطلوبان")
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": str(uuid.uuid4()),
        "name_en": name_en,
        "name_ar": (data.get("name_ar") or name_en).strip(),
        "code": code,
        "icao_code": (data.get("icao_code") or None),
        "iata_code": (data.get("iata_code") or None),
        "country": (data.get("country") or None),
        "is_active": bool(data.get("is_active", True)),
        "created_at": now, "updated_at": now,
    }
    try:
        res = sb.table("companies").insert(row).execute()
    except Exception as e:
        raise ConflictError(f"تعذّر الإنشاء (الكود مكرر؟): {str(e)[:120]}")
    return res.data[0] if res.data else row


@router.patch("/{company_id}")
async def update_company(company_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Edit fields or toggle is_active (activate / deactivate). No delete by design."""
    _ensure_manager(current_user)
    patch = {"updated_at": datetime.now(timezone.utc).isoformat()}
    for k in ("name_en", "name_ar", "code", "icao_code", "iata_code", "country", "is_active"):
        if k in data:
            patch[k] = data[k]
    res = sb.table("companies").update(patch).eq("id", company_id).execute()
    if not res.data:
        raise NotFoundError("Company", company_id)
    return res.data[0]
