"""Per-company operational settings — batch 1 (loader + endpoints only).

NOTHING operational consumes these values yet: the publish/finalize/GD/assign
gates still read their original constants. Wiring happens in later batches,
each behind its own equivalence tests (docs/COMPANY_SETTINGS_PLAN.md).
"""
import json
import uuid
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.api.deps import SbClient, CurrentUser
from app.core.audit import write_audit
from app.core.company_settings import (
    DEFAULTS, KNOWN_KEYS, effective_settings, invalidate_settings_cache,
    validate_setting,
)
from app.core.exceptions import ForbiddenError

router = APIRouter(prefix="/settings", tags=["Operational Settings"])
logger = logging.getLogger(__name__)

_SETTINGS_ADMINS = {"super_admin", "admin"}


def _ensure_settings_admin(user: dict) -> None:
    if user.get("role") not in _SETTINGS_ADMINS and not user.get("is_superuser"):
        raise ForbiddenError("إعدادات الشركة التشغيلية للإدارة فقط")


@router.get("/operational")
async def get_operational_settings(current_user: CurrentUser, sb: SbClient):
    """Every known key with its EFFECTIVE value, the code default, and whether
    the company customized it."""
    _ensure_settings_admin(current_user)
    return {
        "company_id": current_user["company_id"],
        "settings": effective_settings(sb, current_user["company_id"]),
    }


@router.put("/operational/{key}")
async def put_operational_setting(key: str, data: dict,
                                  current_user: CurrentUser, sb: SbClient):
    """Set (or override) one operational setting for the caller's company.
    Strict per-key validation; full before/after audit; cache invalidated."""
    _ensure_settings_admin(current_user)
    cid = current_user["company_id"]

    if key not in KNOWN_KEYS:
        raise HTTPException(status_code=422, detail=f"مفتاح غير معروف: {key}")
    if "value" not in data:
        raise HTTPException(status_code=422, detail="الحقل value مطلوب")
    value = data["value"]
    try:
        validate_setting(key, value)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Before-state (stored override if any — None means "was on default").
    existing = (sb.table("settings").select("id, value")
                .eq("company_id", cid).eq("key", key).execute().data) or []
    before_stored = None
    if existing:
        try:
            before_stored = json.loads(existing[0]["value"])
        except (TypeError, ValueError):
            before_stored = existing[0].get("value")

    now = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(value, ensure_ascii=False)
    if existing:
        sb.table("settings").update({"value": encoded, "updated_at": now}) \
            .eq("id", existing[0]["id"]).execute()
    else:
        sb.table("settings").insert({
            "id": str(uuid.uuid4()),
            "company_id": cid,
            "key": key,
            "value": encoded,
            "description": "operational setting (managed via /settings/operational)",
            "created_at": now,
            "updated_at": now,
        }).execute()

    invalidate_settings_cache(cid)

    write_audit(sb, current_user, "operational_setting_updated", "setting", key,
                before={"key": key,
                        "stored_value": before_stored,
                        "was_default": before_stored is None},
                after={"key": key, "value": value})

    return {
        "key": key,
        "value": value,
        "default": DEFAULTS[key],
        "customized": True,
    }
