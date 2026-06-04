"""Developer Control Center — Vercel integration (deployments + logs).

The backend proxies a small slice of the Vercel REST API so the dashboard
can list deployments, promote a previous one (rollback), and read runtime
logs by request_id — all without the user opening vercel.com.

Auth: requires a Vercel API token in env `VERCEL_API_TOKEN`. Optional
`VERCEL_TEAM_ID` and `VERCEL_PROJECT_NAME` are forwarded as query params
when present. We NEVER return the token to the client; only the proxied
result.

This file is intentionally minimal — the heavy work lives in Vercel; we
add the developer-role gate, basic field whitelisting (no Vercel internal
ids leak), and audit logging for promotions.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/developer/vercel", tags=["Developer — Vercel"])

_VERCEL_API = "https://api.vercel.com"


def _ensure_developer(user: dict) -> None:
    if user.get("role") != "developer" and not user.get("is_superuser"):
        raise ForbiddenError("Developer role required")


def _vercel_headers() -> dict[str, str]:
    token = os.getenv("VERCEL_API_TOKEN")
    if not token:
        raise HTTPException(
            status_code=501,
            detail="VERCEL_API_TOKEN not set on this backend. Add it in "
                   "Vercel dashboard → Settings → Environment Variables, "
                   "then redeploy.",
        )
    return {"Authorization": f"Bearer {token}"}


def _team_param() -> str:
    team = os.getenv("VERCEL_TEAM_ID") or os.getenv("VERCEL_TEAM_SLUG")
    return f"?teamId={urllib.parse.quote(team)}" if team else ""


# ── Deployments ────────────────────────────────────────────────────
@router.get("/deployments")
async def list_deployments(current_user: CurrentUser,
                            project: Optional[str] = None, limit: int = 10):
    """Recent deployments for one Vercel project. Defaults to whatever
    VERCEL_PROJECT_NAME is set to in env (typically 'backend')."""
    _ensure_developer(current_user)
    proj = project or os.getenv("VERCEL_PROJECT_NAME", "backend")
    limit = max(1, min(limit, 30))
    qs = _team_param()
    sep = "&" if qs else "?"
    url = f"{_VERCEL_API}/v6/deployments{qs}{sep}app={urllib.parse.quote(proj)}&limit={limit}"
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(url, headers=_vercel_headers())
        r.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:200])
    body = r.json()
    # Whitelist the fields the UI needs.
    items = [{
        "uid":       d.get("uid"),
        "name":      d.get("name"),
        "url":       d.get("url"),
        "state":     d.get("state") or d.get("readyState"),
        "target":    d.get("target"),
        "created":   d.get("createdAt") or d.get("created"),
        "ready":     d.get("ready"),
        "creator":   (d.get("creator") or {}).get("username"),
        "meta_branch": (d.get("meta") or {}).get("githubCommitRef"),
        "meta_msg":  (d.get("meta") or {}).get("githubCommitMessage"),
    } for d in (body.get("deployments") or [])]
    return {"project": proj, "items": items}


@router.post("/deployments/{deployment_id}/promote")
async def promote_deployment(deployment_id: str,
                              current_user: CurrentUser, sb: SbClient,
                              project: Optional[str] = None):
    """Promote a previous deployment to production (rollback). Audited."""
    _ensure_developer(current_user)
    proj = project or os.getenv("VERCEL_PROJECT_NAME", "backend")
    # Vercel's promote endpoint is per-project.
    qs = _team_param()
    url = (f"{_VERCEL_API}/v10/projects/{urllib.parse.quote(proj)}"
           f"/promote/{urllib.parse.quote(deployment_id)}{qs}")
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(url, headers=_vercel_headers())
        r.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:200])
    # Audit every promotion — rollbacks are sensitive.
    try:
        import json as _json
        sb.table("audit_log").insert({
            "user_id":     current_user["id"],
            "user_name":   current_user.get("name_ar") or current_user.get("email"),
            "action":      "vercel_deployment_promoted",
            "entity_type": "deployment",
            "entity_id":   deployment_id,
            "after_data":  _json.dumps({"project": proj}, ensure_ascii=False),
            "company_id":  current_user.get("company_id"),
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning("audit log failed for promotion: %s", e)
    return {"ok": True, "deployment_id": deployment_id, "project": proj}


# ── Runtime logs (best-effort grep by request_id) ─────────────────
@router.get("/logs")
async def deployment_logs(current_user: CurrentUser,
                           deployment_id: Optional[str] = None,
                           limit: int = 100):
    """Most recent runtime logs for a deployment.

    Vercel's "Runtime Logs" API requires a paid plan + the `vercel logs`
    permission. On Hobby plans it 403s. We surface that cleanly so the UI
    can fall back to the `vercel logs` CLI instructions instead of silently
    showing an empty list.
    """
    _ensure_developer(current_user)
    if not deployment_id:
        # Default: log of the current production deploy (look it up first)
        proj = os.getenv("VERCEL_PROJECT_NAME", "backend")
        qs = _team_param()
        sep = "&" if qs else "?"
        list_url = f"{_VERCEL_API}/v6/deployments{qs}{sep}app={urllib.parse.quote(proj)}&limit=1&target=production"
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(list_url, headers=_vercel_headers())
            r.raise_for_status()
            deps = r.json().get("deployments") or []
            if not deps:
                return {"items": [], "note": "no production deployment found"}
            deployment_id = deps[0].get("uid")
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)[:200])

    url = f"{_VERCEL_API}/v2/deployments/{urllib.parse.quote(deployment_id)}/events{_team_param()}"
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(url, headers=_vercel_headers(),
                              params={"limit": max(1, min(limit, 1000))})
        if r.status_code == 403:
            return {"items": [], "blocked": True,
                    "note": "Vercel runtime logs require Pro plan. "
                            "Use `vercel logs <url>` from CLI instead."}
        r.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:200])
    body = r.json()
    # Body shape varies — pass through with light shaping.
    raw_items = body if isinstance(body, list) else (body.get("events") or body.get("items") or [])
    items = [{
        "type":   it.get("type") or it.get("level"),
        "text":   it.get("text") or it.get("payload", {}).get("text") or str(it)[:500],
        "ts":     it.get("created") or it.get("date") or it.get("timestamp"),
    } for it in raw_items]
    return {"deployment_id": deployment_id, "items": items}
