"""Operations Manual articles — admin-editable CRUD.

Read access is per-company and the visible_to_roles filter is applied
server-side so the client never receives clauses it shouldn't see.
Write access is admin / ops_manager only.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SbClient
from app.core.exceptions import ForbiddenError, NotFoundError

router = APIRouter(prefix="/om-articles", tags=["OM"])
log = logging.getLogger(__name__)

_EDITOR_ROLES = {"super_admin", "admin", "ops_manager"}
_ALLOWED_FIELDS = {
    "section", "chapter_ar", "chapter_en", "title_ar", "title_en",
    "body_ar", "body_en", "visible_to_roles", "linked_rule_id",
    "linked_route", "sort_order", "is_active",
}


def _ensure_editor(user: dict) -> None:
    if user.get("role") not in _EDITOR_ROLES:
        raise ForbiddenError("Only admin / ops_manager can edit the Operations Manual")


@router.get("")
async def list_articles(current_user: CurrentUser, sb: SbClient):
    """Return all OM articles visible to the caller's role + company.

    Filtering is intentionally done in Python rather than at SQL — the
    `visible_to_roles` array stores an empty list to mean "everyone",
    and PostgREST array filters can't express that cleanly. The table
    is small (a few dozen rows), so this is fine.
    """
    company_id = current_user["company_id"]
    res = sb.table("om_articles") \
        .select("*") \
        .eq("company_id", company_id) \
        .eq("is_active", True) \
        .order("section") \
        .order("sort_order") \
        .order("id") \
        .execute()

    role = current_user.get("role", "")
    out = []
    for row in (res.data or []):
        roles = row.get("visible_to_roles") or []
        if not roles or role in roles:
            out.append(row)
    return out


@router.post("", status_code=201)
async def create_article(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)

    art_id = (data.get("id") or "").strip()
    if not art_id:
        raise HTTPException(status_code=422, detail="id is required (e.g. 'OM-A 9.1')")

    section = (data.get("section") or "").strip().upper()
    if section not in {"A", "B", "C", "D"}:
        raise HTTPException(status_code=422, detail="section must be one of A/B/C/D")

    # Duplicate check
    existing = sb.table("om_articles").select("id").eq("id", art_id).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail=f"Article '{art_id}' already exists")

    now = datetime.now(timezone.utc).isoformat()
    row = {k: v for k, v in data.items() if k in _ALLOWED_FIELDS}
    row.update({
        "id":            art_id,
        "section":       section,
        "company_id":    current_user["company_id"],
        "title_ar":      data.get("title_ar", ""),
        "title_en":      data.get("title_en", ""),
        "body_ar":       data.get("body_ar", ""),
        "body_en":       data.get("body_en", ""),
        "created_by":    current_user["id"],
        "updated_by":    current_user["id"],
        "created_at":    now,
        "updated_at":    now,
    })

    if not row["title_ar"] and not row["title_en"]:
        raise HTTPException(status_code=422, detail="At least one title (ar/en) is required")

    try:
        res = sb.table("om_articles").insert(row).execute()
    except Exception as e:
        log.exception("create OM article failed")
        raise HTTPException(status_code=502, detail=f"insert failed: {str(e)[:200]}")
    return res.data[0] if res.data else row


@router.patch("/{article_id:path}")
async def update_article(
    article_id: str, data: dict, current_user: CurrentUser, sb: SbClient
):
    _ensure_editor(current_user)

    company_id = current_user["company_id"]
    existing = sb.table("om_articles") \
        .select("id") \
        .eq("id", article_id) \
        .eq("company_id", company_id) \
        .execute()
    if not existing.data:
        raise NotFoundError("OM article", article_id)

    update = {k: v for k, v in data.items() if k in _ALLOWED_FIELDS}
    update["updated_by"] = current_user["id"]

    if "section" in update:
        update["section"] = (update["section"] or "").strip().upper()
        if update["section"] not in {"A", "B", "C", "D"}:
            raise HTTPException(status_code=422, detail="section must be one of A/B/C/D")

    try:
        res = sb.table("om_articles") \
            .update(update) \
            .eq("id", article_id) \
            .eq("company_id", company_id) \
            .execute()
    except Exception as e:
        log.exception("update OM article failed")
        raise HTTPException(status_code=502, detail=f"update failed: {str(e)[:200]}")
    return res.data[0] if res.data else {}


@router.delete("/{article_id:path}", status_code=204)
async def delete_article(article_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)

    company_id = current_user["company_id"]
    res = sb.table("om_articles") \
        .delete() \
        .eq("id", article_id) \
        .eq("company_id", company_id) \
        .execute()
    if not res.data:
        raise NotFoundError("OM article", article_id)
    return None


@router.post("/seed", status_code=201)
async def seed_defaults(data: dict, current_user: CurrentUser, sb: SbClient):
    """Bulk-import the static catalog from the client.

    The client sends `articles: [...]`; we upsert by id so re-seeding is
    safe and only fills in missing rows. Used the first time an admin
    opens the Settings page to populate the table from the ship-with-binary
    defaults.
    """
    _ensure_editor(current_user)

    articles = data.get("articles") or []
    if not isinstance(articles, list) or not articles:
        raise HTTPException(status_code=422, detail="articles[] is required")

    company_id = current_user["company_id"]
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for a in articles:
        rows.append({
            "id":               a.get("id"),
            "company_id":       company_id,
            "section":          (a.get("section") or "A").strip().upper(),
            "chapter_ar":       a.get("chapter_ar", ""),
            "chapter_en":       a.get("chapter_en", ""),
            "title_ar":         a.get("title_ar", ""),
            "title_en":         a.get("title_en", ""),
            "body_ar":          a.get("body_ar", ""),
            "body_en":          a.get("body_en", ""),
            "visible_to_roles": a.get("visible_to_roles") or [],
            "linked_rule_id":   a.get("linked_rule_id"),
            "linked_route":     a.get("linked_route"),
            "sort_order":       a.get("sort_order", 0),
            "is_active":        True,
            "created_by":       current_user["id"],
            "updated_by":       current_user["id"],
            "created_at":       now,
            "updated_at":       now,
        })

    inserted = 0
    skipped  = 0
    for r in rows:
        try:
            existing = sb.table("om_articles").select("id").eq("id", r["id"]).execute()
            if existing.data:
                skipped += 1
                continue
            sb.table("om_articles").insert(r).execute()
            inserted += 1
        except Exception:
            log.exception("seed row failed for %s", r.get("id"))
    return {"inserted": inserted, "skipped": skipped, "total": len(rows)}
