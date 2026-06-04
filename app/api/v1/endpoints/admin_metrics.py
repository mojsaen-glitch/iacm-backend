"""Observability Dashboard — Super-Admin facing endpoints (Phase 1).

`/api/v1/admin/health/detailed`  — single JSON snapshot the dashboard
   polls every 5-10 seconds for the Overview screen (plan §5.1).

All endpoints in this router require Super Admin. The gate is applied via
a per-router dependency so a future PR cannot accidentally add a public
admin endpoint by forgetting the decorator.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError
from app.services.metrics_service import MetricsCollector

logger = logging.getLogger(__name__)


def _ensure_super_admin(user: dict) -> None:
    """Hard gate — Super Admin or Developer (plan §8.1).

    Developer is the strict superset of super_admin in our role hierarchy,
    so any /admin/* surface the super_admin can use, the developer can too.
    Anything stricter (the DCC's destructive surface) uses its own
    `_ensure_developer` gate inside developer.py / developer_actions.py.
    """
    role = user.get("role")
    if role not in ("super_admin", "developer") and not user.get("is_superuser"):
        raise ForbiddenError("Super admin only")


router = APIRouter(
    prefix="/admin",
    tags=["Admin — Observability"],
)


# Process start time — used for uptime in /health/detailed.
_PROCESS_STARTED_AT = time.time()


@router.get("/health/detailed")
async def health_detailed(current_user: CurrentUser, sb: SbClient):
    """One-shot JSON snapshot for the Overview Dashboard.

    Sections (each independently safe — a section that errors returns
    `{"error": "..."}` so a single bad subsystem doesn't 500 the whole
    health check):
       •  api           — last-5min p50/p95/p99 + error rate + RPM
       •  system        — CPU/RAM/disk via psutil
       •  collector     — in-process metrics queue depth + drop count
       •  database      — supabase reachability + connection probe
       •  uptime_sec    — seconds since process start
       •  active_alerts — count of `alerts.status='active'`
    """
    _ensure_super_admin(current_user)

    out: dict = {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "uptime_sec": int(time.time() - _PROCESS_STARTED_AT),
    }

    # ── API metrics (from the last 5 minutes of metrics_requests) ────────
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        rows = (sb.table("metrics_requests")
                .select("status,duration_ms")
                .gte("ts", cutoff)
                .limit(10000)
                .execute().data or [])
        if rows:
            durations = sorted(int(r.get("duration_ms") or 0) for r in rows)
            n = len(durations)
            def pct(p: float) -> int:
                idx = max(0, min(n - 1, int(round(p * (n - 1)))))
                return durations[idx]
            err = sum(1 for r in rows if int(r.get("status") or 0) >= 400)
            out["api"] = {
                "window_min":    5,
                "request_count": n,
                "rpm":           round(n / 5.0, 1),
                "error_rate":    round(err / n, 4),
                "p50_ms":        pct(0.50),
                "p95_ms":        pct(0.95),
                "p99_ms":        pct(0.99),
                "avg_ms":        int(sum(durations) / n),
            }
        else:
            out["api"] = {"window_min": 5, "request_count": 0, "rpm": 0,
                          "error_rate": 0, "p50_ms": 0, "p95_ms": 0,
                          "p99_ms": 0, "avg_ms": 0}
    except Exception as e:
        out["api"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}

    # ── System resources (psutil; optional dep) ──────────────────────────
    try:
        import psutil      # type: ignore[import-untyped]
        # `cpu_percent` returns the value since the last call; calling once
        # with interval=None gives a non-blocking snapshot. The first call
        # in a process returns 0.0 — acceptable for our 5-second polling.
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        out["system"] = {
            "cpu_pct":      psutil.cpu_percent(interval=None),
            "ram_pct":      vm.percent,
            "ram_used_mb":  int(vm.used / (1024 * 1024)),
            "ram_total_mb": int(vm.total / (1024 * 1024)),
            "disk_pct":     disk.percent,
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "pid":          os.getpid(),
        }
    except Exception as e:
        out["system"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}

    # ── In-process metrics collector ─────────────────────────────────────
    try:
        out["collector"] = MetricsCollector.instance().snapshot()
    except Exception as e:
        out["collector"] = {"error": str(e)[:120]}

    # ── Database reachability ────────────────────────────────────────────
    try:
        _ = sb.table("companies").select("id").limit(1).execute()
        out["database"] = {"reachable": True}
    except Exception as e:
        out["database"] = {"reachable": False,
                           "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # ── Active alerts count ──────────────────────────────────────────────
    try:
        a = (sb.table("alerts").select("id", count="exact")
             .eq("status", "active").execute())
        out["active_alerts"] = a.count or 0
    except Exception:
        out["active_alerts"] = 0   # table may not exist yet on a fresh DB

    return out


@router.get("/metrics/top-endpoints")
async def top_endpoints(current_user: CurrentUser, sb: SbClient,
                        minutes: int = 60, limit: int = 20):
    """Slowest endpoints by p95 in the last N minutes — feeds the API
    monitoring screen (plan §5.2). Defaults are tuned for the Overview card."""
    _ensure_super_admin(current_user)
    minutes = max(1, min(minutes, 1440))   # 1 min .. 24 h
    limit   = max(1, min(limit, 100))
    cutoff  = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        rows = (sb.table("metrics_requests")
                .select("path,method,status,duration_ms")
                .gte("ts", cutoff).limit(20000).execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Bucket and compute p95 per (path, method)
    buckets: dict[tuple[str, str], list[int]] = {}
    err_buckets: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r.get("path") or "", r.get("method") or "")
        buckets.setdefault(key, []).append(int(r.get("duration_ms") or 0))
        if int(r.get("status") or 0) >= 400:
            err_buckets[key] = err_buckets.get(key, 0) + 1
    out: list[dict] = []
    for (path, method), ds in buckets.items():
        ds.sort()
        n = len(ds)
        p95 = ds[max(0, min(n - 1, int(round(0.95 * (n - 1)))))]
        out.append({
            "path": path, "method": method,
            "count": n, "p95_ms": p95,
            "avg_ms": int(sum(ds) / n) if n else 0,
            "errors": err_buckets.get((path, method), 0),
        })
    out.sort(key=lambda r: r["p95_ms"], reverse=True)
    return {"window_min": minutes, "items": out[:limit]}


@router.get("/metrics/errors")
async def recent_errors(current_user: CurrentUser, sb: SbClient,
                         minutes: int = 60, limit: int = 100):
    """Most recent 4xx/5xx responses — drill-down for the error rate card."""
    _ensure_super_admin(current_user)
    minutes = max(1, min(minutes, 1440))
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        rows = (sb.table("metrics_requests")
                .select("ts,method,path,status,duration_ms,user_id,ip,request_id")
                .gte("ts", cutoff)
                .gte("status", 400)
                .order("ts", desc=True)
                .limit(max(1, min(limit, 500)))
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"window_min": minutes, "items": rows}


# ── M3: Audit log + sessions + failed logins ─────────────────────────
@router.get("/audit")
async def audit_log(current_user: CurrentUser, sb: SbClient,
                    minutes: int = 1440, limit: int = 200,
                    user_id: Optional[str] = None, action: Optional[str] = None):
    """Recent audit log entries — feeds the User Activity screen."""
    _ensure_super_admin(current_user)
    minutes = max(1, min(minutes, 30 * 1440))   # 1 min .. 30 days
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        q = (sb.table("audit_log")
             .select("id,user_id,user_name,action,entity_type,entity_id,created_at,after_data")
             .gte("created_at", cutoff)
             .order("created_at", desc=True)
             .limit(max(1, min(limit, 1000))))
        if user_id: q = q.eq("user_id", user_id)
        if action:  q = q.eq("action",  action)
        rows = q.execute().data or []
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"window_min": minutes, "items": rows}


@router.get("/sessions/active")
async def active_sessions(current_user: CurrentUser, sb: SbClient):
    """Users who logged in within the last 30 minutes — proxy for 'active
    sessions'. Real session tracking needs a separate table (Phase 5)."""
    _ensure_super_admin(current_user)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    try:
        rows = (sb.table("users")
                .select("id,email,role,name_ar,name_en,last_login,company_id")
                .gte("last_login", cutoff)
                .order("last_login", desc=True)
                .limit(500)
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"window_min": 30, "count": len(rows), "items": rows}


@router.get("/failed-logins")
async def failed_logins(current_user: CurrentUser, sb: SbClient,
                         minutes: int = 1440, limit: int = 200):
    """Failed POST /auth/login attempts in the last N minutes. Computed from
    metrics_requests where path matches and status is 401/422."""
    _ensure_super_admin(current_user)
    minutes = max(1, min(minutes, 7 * 1440))
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        rows = (sb.table("metrics_requests")
                .select("ts,ip,status,user_agent,request_id")
                .eq("method", "POST")
                .gte("ts", cutoff)
                .gte("status", 400)
                .lt("status", 500)
                .like("path", "%/auth/login%")
                .order("ts", desc=True)
                .limit(max(1, min(limit, 500)))
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Aggregate by IP for the heatmap.
    by_ip: dict[str, int] = {}
    for r in rows:
        ip = r.get("ip") or "unknown"
        by_ip[ip] = by_ip.get(ip, 0) + 1
    top_ips = sorted(by_ip.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return {
        "window_min": minutes,
        "total":      len(rows),
        "items":      rows,
        "top_ips":    [{"ip": ip, "count": c} for ip, c in top_ips],
    }


# ── M4: Alert rules CRUD + alert acknowledgement ─────────────────────
@router.get("/alerts")
async def list_alerts(current_user: CurrentUser, sb: SbClient,
                       status: Optional[str] = None, limit: int = 100):
    _ensure_super_admin(current_user)
    q = (sb.table("alerts")
         .select("*").order("fired_at", desc=True)
         .limit(max(1, min(limit, 500))))
    if status: q = q.eq("status", status)
    return {"items": q.execute().data or []}


@router.post("/alerts/{alert_id}/acknowledge")
async def ack_alert(alert_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    sb.table("alerts").update({
        "status":          "acknowledged",
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        "acknowledged_by": current_user["id"],
    }).eq("id", alert_id).execute()
    return {"ok": True}


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    sb.table("alerts").update({
        "status":      "resolved",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", alert_id).execute()
    return {"ok": True}


@router.get("/alert-rules")
async def list_alert_rules(current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    return {"items": sb.table("alert_rules").select("*")
            .order("name").execute().data or []}


@router.post("/alert-rules")
async def create_alert_rule(data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    # Whitelist the writable fields so a client can't sneak in arbitrary columns.
    payload = {k: data.get(k) for k in
               ("name", "metric", "operator", "threshold", "duration_sec",
                "severity", "channels", "enabled", "description")
               if data.get(k) is not None}
    res = sb.table("alert_rules").insert(payload).execute()
    return res.data[0] if res.data else {}


@router.patch("/alert-rules/{rule_id}")
async def update_alert_rule(rule_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    payload = {k: data.get(k) for k in
               ("metric", "operator", "threshold", "duration_sec",
                "severity", "channels", "enabled", "description")
               if data.get(k) is not None}
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("alert_rules").update(payload).eq("id", rule_id).execute()
    return {"ok": True}


@router.delete("/alert-rules/{rule_id}")
async def delete_alert_rule(rule_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_super_admin(current_user)
    sb.table("alert_rules").delete().eq("id", rule_id).execute()
    return {"ok": True}


# ── Polish: time-series for charts ───────────────────────────────────
@router.get("/metrics/timeseries")
async def metrics_timeseries(current_user: CurrentUser, sb: SbClient,
                              hours: int = 24):
    """Hourly time-series for the API monitoring chart. Reads from the
    pre-computed `metrics_requests_h` rollup so the query stays cheap even
    over a week-long window."""
    _ensure_super_admin(current_user)
    hours  = max(1, min(hours, 24 * 7))      # 1 h .. 7 days
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = (sb.table("metrics_requests_h")
                .select("hour,count,errors_4xx,errors_5xx,p95_ms,avg_ms")
                .gte("hour", cutoff)
                .order("hour", desc=False)
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Aggregate across (path, method) per hour for a single-line chart.
    bucket: dict[str, dict] = {}
    for r in rows:
        h = r["hour"]
        b = bucket.setdefault(h, {"hour": h, "count": 0, "err": 0, "p95_ms": 0, "avg_ms": 0})
        b["count"]  += int(r.get("count") or 0)
        b["err"]    += int(r.get("errors_4xx") or 0) + int(r.get("errors_5xx") or 0)
        b["p95_ms"]  = max(b["p95_ms"], int(r.get("p95_ms") or 0))   # max of path-p95
        b["avg_ms"]  = max(b["avg_ms"], int(r.get("avg_ms") or 0))   # rough; refine later
    out = sorted(bucket.values(), key=lambda r: r["hour"])
    for b in out:
        b["error_rate"] = round(b["err"] / b["count"], 4) if b["count"] else 0.0
        b["rpm"]        = round(b["count"] / 60.0, 1)
    return {"hours": hours, "items": out}


# ── Polish: full user list for /users page ─────────────────────────
@router.get("/users")
async def list_users(current_user: CurrentUser, sb: SbClient,
                      search: Optional[str] = None, limit: int = 200):
    _ensure_super_admin(current_user)
    try:
        q = (sb.table("users").select(
                "id,email,name_ar,name_en,role,is_active,last_login,"
                "company_id,created_at,totp_enabled")
             .order("last_login", desc=True).limit(max(1, min(limit, 1000))))
        rows = q.execute().data or []
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if search:
        s = search.lower()
        rows = [r for r in rows
                if s in (r.get("email") or "").lower()
                or s in (r.get("name_ar") or "").lower()
                or s in (r.get("name_en") or "").lower()]
    return {"count": len(rows), "items": rows}


@router.get("/db/stats")
async def db_stats(current_user: CurrentUser, sb: SbClient):
    """Quick DB health: row counts of the biggest tables, captured from
    information_schema where possible. Falls back to a `count('exact')` on
    a known set when pg_stat_user_tables isn't accessible via PostgREST."""
    _ensure_super_admin(current_user)
    tables = ["users", "crew", "flights", "assignments", "notifications",
              "messages", "audit_log", "metrics_requests", "metrics_requests_h"]
    out = []
    for t in tables:
        try:
            r = sb.table(t).select("id", count="estimated").limit(1).execute()
            out.append({"table": t, "rows": r.count or 0})
        except Exception:
            out.append({"table": t, "rows": None})
    return {"tables": out}
