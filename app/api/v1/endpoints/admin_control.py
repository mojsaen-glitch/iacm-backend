"""M5 — Full-control admin endpoints.

Feature flags · maintenance mode · force-logout · enable/disable user.

The safe-SQL endpoint was removed at the user's request — direct DB access
remains available through Supabase Studio's SQL Editor, which already has
its own audit trail and ownership boundaries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin — Control"])


def _ensure_super_admin(user: dict) -> None:
    """Super admin OR developer (mirrors admin_metrics._ensure_super_admin)."""
    role = user.get("role")
    if role not in ("super_admin", "developer") and not user.get("is_superuser"):
        raise ForbiddenError("Super admin only")


# ── Feature flags ────────────────────────────────────────────────────
@router.get("/feature-flags")
async def list_flags(current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    return {"items": sb.table("feature_flags").select("*")
            .order("key").execute().data or []}


class _FlagUpdate(BaseModel):
    enabled: bool


@router.patch("/feature-flags/{key}")
async def toggle_flag(key: str, data: _FlagUpdate,
                       current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    sb.table("feature_flags").update({
        "enabled":    data.enabled,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": current_user["id"],
    }).eq("key", key).execute()
    # Audit trail — every flip is recorded.
    _audit(sb, current_user, "feature_flag_toggle", "feature_flag", key,
           {"enabled": data.enabled})
    return {"ok": True, "key": key, "enabled": data.enabled}


# ── Maintenance mode ─────────────────────────────────────────────────
@router.get("/maintenance")
async def get_maintenance(current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    res = sb.table("system_config").select("value") \
        .eq("key", "maintenance_mode").limit(1).execute()
    return res.data[0]["value"] if res.data else {"enabled": False}


class _MaintToggle(BaseModel):
    enabled: bool
    message: Optional[str] = None
    allow_super_admin: Optional[bool] = True


@router.put("/maintenance")
async def set_maintenance(data: _MaintToggle,
                           current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    value = {
        "enabled":           bool(data.enabled),
        "message":           data.message or "النظام تحت الصيانة المؤقتة",
        "allow_super_admin": bool(data.allow_super_admin if data.allow_super_admin is not None else True),
    }
    sb.table("system_config").upsert({
        "key":        "maintenance_mode",
        "value":      value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": current_user["id"],
    }).execute()
    _audit(sb, current_user, "maintenance_mode_toggle",
           "system_config", "maintenance_mode", value)
    return value


# ── Force logout ─────────────────────────────────────────────────────
@router.post("/users/{user_id}/force-logout")
async def force_logout(user_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    # Clearing refresh_token invalidates the refresh path; the access token
    # expires within the existing JWT TTL (60 min). For instant revocation
    # we'd need a token deny-list — out of scope here.
    sb.table("users").update({"refresh_token": None}).eq("id", user_id).execute()
    _audit(sb, current_user, "force_logout", "user", user_id, {})
    return {"ok": True, "user_id": user_id}


class _UserActiveUpdate(BaseModel):
    is_active: bool


@router.patch("/users/{user_id}/active")
async def set_user_active(user_id: str, data: _UserActiveUpdate,
                          current_user: CurrentUser, sb: SbClient):
    """Disable / enable a user account. Disabling also wipes the refresh
    token so they're kicked out within the access-token TTL."""
    _ensure_super_admin(current_user)
    if user_id == current_user["id"] and not data.is_active:
        raise ForbiddenError("لا يمكنك تعطيل حسابك الخاص")
    update = {"is_active": data.is_active}
    if not data.is_active:
        update["refresh_token"] = None
    sb.table("users").update(update).eq("id", user_id).execute()
    _audit(sb, current_user,
           "user_enabled" if data.is_active else "user_disabled",
           "user", user_id, {"is_active": data.is_active})
    return {"ok": True, "user_id": user_id, "is_active": data.is_active}


# ── audit helper ─────────────────────────────────────────────────────
def _audit(sb, current_user: dict, action: str, entity_type: str,
           entity_id: str, after: dict) -> None:
    try:
        sb.table("audit_log").insert({
            "user_id":     current_user["id"],
            "user_name":   current_user.get("name_ar") or current_user.get("email"),
            "action":      action,
            "entity_type": entity_type,
            "entity_id":   entity_id,
            "after_data":  __import__("json").dumps(after, ensure_ascii=False),
            "company_id":  current_user.get("company_id"),
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning("audit log insert failed (%s): %s", action, e)
