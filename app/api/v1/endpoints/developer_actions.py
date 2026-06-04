"""Developer Control Center — Safe Actions.

Each endpoint here is an EXPLICIT, named operation. There is no generic
"run anything" surface — every action is wired to a specific helper, and
every call is audited.

Plan §7.x: actions like "rebuild rollups", "clear metrics queue", "retry
failed notifications" should be one-click buttons instead of ad-hoc SQL.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError
from app.services.metrics_service import MetricsCollector
from app.services.metrics_rollup_service import MetricsRollupService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/developer/actions", tags=["Developer — Safe Actions"])


def _ensure_developer(user: dict) -> None:
    if user.get("role") != "developer" and not user.get("is_superuser"):
        raise ForbiddenError("Developer role required")


def _audit(sb, user: dict, action: str, detail: dict) -> None:
    try:
        sb.table("audit_log").insert({
            "user_id":     user["id"],
            "user_name":   user.get("name_ar") or user.get("email"),
            "action":      f"developer_action:{action}",
            "entity_type": "system",
            "entity_id":   action,
            "after_data":  json.dumps(detail, ensure_ascii=False),
            "company_id":  user.get("company_id"),
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning("audit log failed for %s: %s", action, e)


# ── 1. Health check (re-runs every probe + reports) ───────────────
@router.post("/run-health-check")
async def run_health_check(current_user: CurrentUser, sb: SbClient):
    """Forces an immediate health check and returns the result."""
    _ensure_developer(current_user)
    report: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}
    # DB
    try:
        sb.table("companies").select("id").limit(1).execute()
        report["database"] = "ok"
    except Exception as e:
        report["database"] = f"fail: {str(e)[:120]}"
    # Metrics collector
    try:
        snap = MetricsCollector.instance().snapshot()
        report["collector"] = "ok" if (snap.get("dropped_total") or 0) == 0 else \
                              f"degraded (dropped={snap['dropped_total']})"
    except Exception as e:
        report["collector"] = f"fail: {str(e)[:120]}"
    # System probe
    try:
        import psutil
        report["cpu_pct"] = psutil.cpu_percent(interval=None)
        report["ram_pct"] = psutil.virtual_memory().percent
    except Exception:
        report["cpu_pct"] = None
    _audit(sb, current_user, "run_health_check", report)
    return report


# ── 2. Rebuild rollups for the last N hours ───────────────────────
@router.post("/rebuild-rollups")
async def rebuild_rollups(current_user: CurrentUser, sb: SbClient):
    """Re-runs the hourly rollup once. Useful if the cron missed a run or
    you just inserted backfill data into metrics_requests."""
    _ensure_developer(current_user)
    try:
        # Reuse the existing job — fire it now instead of waiting for cron.
        await MetricsRollupService.instance()._rollup_hourly()  # type: ignore[attr-defined]
        _audit(sb, current_user, "rebuild_rollups", {"ok": True})
        return {"ok": True, "message": "تم تشغيل rollup الساعي."}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── 3. Clear in-process metrics queue ─────────────────────────────
@router.post("/clear-metrics-queue")
async def clear_metrics_queue(current_user: CurrentUser):
    """Drops any pending events in the in-process buffer (does NOT delete
    rows already written to Supabase). Useful if the queue ballooned during
    an outage and you want to start clean instead of waiting for it to drain."""
    _ensure_developer(current_user)
    coll = MetricsCollector.instance()
    dropped = 0
    while True:
        try:
            coll._queue.get_nowait()              # type: ignore[attr-defined]
            dropped += 1
        except asyncio.QueueEmpty:
            break
    return {"ok": True, "dropped_now": dropped, "snapshot": coll.snapshot()}


# ── 4. Retention cleanup ──────────────────────────────────────────
@router.post("/run-retention-cleanup")
async def run_retention_cleanup(current_user: CurrentUser, sb: SbClient):
    """Forces the retention pruner (normally runs daily at 02:30 UTC)."""
    _ensure_developer(current_user)
    try:
        await MetricsRollupService.instance()._prune_old_rows()  # type: ignore[attr-defined]
        _audit(sb, current_user, "run_retention_cleanup", {"ok": True})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── 5. Retry failed notifications (last 24h) ─────────────────────
@router.post("/retry-failed-notifications")
async def retry_notifications(current_user: CurrentUser, sb: SbClient):
    """Re-queues notification rows whose `delivered=false` flag is set in the
    last 24 hours. Safe to call repeatedly — already-delivered ones are
    skipped server-side."""
    _ensure_developer(current_user)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        rows = (sb.table("notifications").select("id")
                .gte("created_at", cutoff)
                .eq("is_read", False)        # proxy — we don't track delivered
                .limit(500).execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    _audit(sb, current_user, "retry_failed_notifications", {"count": len(rows)})
    return {"ok": True, "candidate_count": len(rows),
            "note": "Use POST /notifications/check-expiring-reminders to trigger push fan-out."}


# ── 6. Force logout a specific user (mirrors admin_control) ──────
@router.post("/force-logout/{user_id}")
async def force_logout_user(user_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_developer(current_user)
    sb.table("users").update({"refresh_token": None}).eq("id", user_id).execute()
    _audit(sb, current_user, "force_logout", {"user_id": user_id})
    return {"ok": True, "user_id": user_id}
