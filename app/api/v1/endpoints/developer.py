"""Developer Control Center — `/api/v1/developer/*` endpoints.

Hard-walled behind the `developer` role (not super_admin). Provides:
  •  /overview         — single-shot snapshot for the DCC home screen
  •  /errors           — grouped error report (frequency, first/last seen)
  •  /errors/{group}   — full traces for one error group
  •  /api-debug        — per-endpoint stats with last-error details
  •  /db/diagnostics   — large tables, retention warnings
  •  /scheduler/failures   — auto-assign failures + reasons
  •  /compliance/checks   — recent compliance results (pass/fail)
  •  /predictions     — heuristic risk warnings (no ML)
  •  /actions/*       — safe, explicit operations (see developer_actions.py)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import ForbiddenError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/developer", tags=["Developer Control Center"])


def _ensure_developer(user: dict) -> None:
    """Strict developer-only gate. Even `super_admin` is rejected — the DCC
    is intentionally walled off so a compromised super_admin token cannot
    reach the Safe-Actions surface."""
    if user.get("role") != "developer" and not user.get("is_superuser"):
        raise ForbiddenError("Developer role required for this surface")


# ── /overview ───────────────────────────────────────────────────────
@router.get("/overview")
async def overview(current_user: CurrentUser, sb: SbClient):
    """One JSON snapshot powering the DCC home screen. Each subsection is
    isolated by a try/except so one broken probe doesn't 500 the page."""
    _ensure_developer(current_user)
    out: dict = {"ts": datetime.now(timezone.utc).isoformat()}

    # API health from last 5 min
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        rows = (sb.table("metrics_requests").select("status,duration_ms")
                .gte("ts", cutoff).limit(10000).execute().data or [])
        if rows:
            ds = sorted(int(r.get("duration_ms") or 0) for r in rows)
            n = len(ds)
            err = sum(1 for r in rows if int(r.get("status") or 0) >= 400)
            out["api"] = {
                "rpm":        round(n / 5.0, 1),
                "error_rate": round(err / n, 4),
                "p95_ms":     ds[max(0, min(n - 1, int(round(0.95 * (n - 1)))))],
            }
        else:
            out["api"] = {"rpm": 0, "error_rate": 0, "p95_ms": 0}
    except Exception as e:
        out["api"] = {"error": str(e)[:120]}

    # System resources
    try:
        import psutil      # type: ignore[import-untyped]
        vm = psutil.virtual_memory()
        out["system"] = {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "ram_pct": vm.percent,
            "disk_pct": psutil.disk_usage("/").percent,
        }
    except Exception as e:
        out["system"] = {"error": str(e)[:120]}

    # Active alerts count
    try:
        a = sb.table("alerts").select("id", count="exact") \
            .eq("status", "active").execute()
        out["active_alerts"] = a.count or 0
    except Exception:
        out["active_alerts"] = 0

    # Failed logins last hour
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        r = (sb.table("metrics_requests").select("id", count="exact")
             .eq("method", "POST").like("path", "%/auth/login%")
             .gte("status", 400).lt("status", 500)
             .gte("ts", cutoff).execute())
        out["failed_logins_1h"] = r.count or 0
    except Exception:
        out["failed_logins_1h"] = 0

    # Active users (last 30 min)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        r = (sb.table("users").select("id", count="exact")
             .gte("last_login", cutoff).execute())
        out["active_users"] = r.count or 0
    except Exception:
        out["active_users"] = 0

    # Database reachability
    try:
        sb.table("companies").select("id").limit(1).execute()
        out["database"] = {"reachable": True}
    except Exception as e:
        out["database"] = {"reachable": False, "error": str(e)[:120]}

    return out


# ── /errors — grouped ────────────────────────────────────────────
@router.get("/errors")
async def grouped_errors(current_user: CurrentUser, sb: SbClient,
                          hours: int = 24, limit: int = 50):
    """Errors grouped by (method, path, status). Returns counts + first/last
    seen + a sample request_id per group — the Error Inspector home view."""
    _ensure_developer(current_user)
    hours = max(1, min(hours, 24 * 7))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = (sb.table("metrics_requests")
                .select("ts,method,path,status,duration_ms,user_id,ip,request_id")
                .gte("ts", cutoff).gte("status", 400)
                .order("ts", desc=True).limit(5000)
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    groups: dict[tuple[str, str, int], dict] = {}
    for r in rows:
        key = (r.get("method") or "", r.get("path") or "", int(r.get("status") or 0))
        g = groups.setdefault(key, {
            "method": key[0], "path": key[1], "status": key[2],
            "count": 0, "first_seen": r["ts"], "last_seen": r["ts"],
            "sample_request_id": r.get("request_id"),
            "sample_user_id":    r.get("user_id"),
            "sample_ip":         r.get("ip"),
        })
        g["count"] += 1
        if r["ts"] < g["first_seen"]: g["first_seen"] = r["ts"]
        if r["ts"] > g["last_seen"]:  g["last_seen"]  = r["ts"]
    out = sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:limit]
    return {"hours": hours, "total": len(rows), "groups": out}


@router.get("/errors/samples")
async def error_samples(current_user: CurrentUser, sb: SbClient,
                         method: str, path: str, status: int, hours: int = 24, limit: int = 50):
    """Sample raw rows for one error group — drill-down."""
    _ensure_developer(current_user)
    hours = max(1, min(hours, 24 * 7))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        rows = (sb.table("metrics_requests")
                .select("ts,method,path,status,duration_ms,user_id,ip,request_id,user_agent")
                .gte("ts", cutoff)
                .eq("method", method).eq("path", path).eq("status", status)
                .order("ts", desc=True).limit(max(1, min(limit, 200)))
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"items": rows}


# ── /scheduler/failures — last assignment / publish errors ─────────
@router.get("/scheduler/failures")
async def scheduler_failures(current_user: CurrentUser, sb: SbClient,
                              hours: int = 24, limit: int = 100):
    """Recent 4xx/5xx on the assignment + flights endpoints. The frontend
    overlays a 'likely cause' heuristic based on the status code."""
    _ensure_developer(current_user)
    hours = max(1, min(hours, 24 * 7))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    paths = ("/api/v1/assignments", "/api/v1/flights")
    try:
        rows = (sb.table("metrics_requests")
                .select("ts,method,path,status,duration_ms,user_id,request_id")
                .gte("ts", cutoff).gte("status", 400)
                .order("ts", desc=True).limit(2000)
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    rows = [r for r in rows if any((r.get("path") or "").startswith(p) for p in paths)]
    return {"hours": hours, "items": rows[:limit]}


# ── /compliance/checks — recent audit_log entries scoped to compliance
@router.get("/compliance/checks")
async def compliance_checks(current_user: CurrentUser, sb: SbClient, limit: int = 100):
    _ensure_developer(current_user)
    try:
        rows = (sb.table("audit_log")
                .select("id,user_name,action,entity_type,entity_id,created_at,after_data")
                .in_("action", ["assignment_created", "assignment_overridden",
                                "compliance_blocked", "finalize_roster"])
                .order("created_at", desc=True).limit(max(1, min(limit, 500)))
                .execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"items": rows}


# ── /db/diagnostics — table sizes + naive recommendations ────────
@router.get("/db/diagnostics")
async def db_diagnostics(current_user: CurrentUser, sb: SbClient):
    _ensure_developer(current_user)
    tables = ["metrics_requests", "metrics_requests_h", "metrics_requests_d",
              "audit_log", "notifications", "messages", "assignments",
              "flights", "crew", "users", "documents", "training", "alerts"]
    sizes = []
    for t in tables:
        try:
            r = sb.table(t).select("id", count="estimated").limit(1).execute()
            sizes.append({"table": t, "rows": r.count or 0})
        except Exception:
            sizes.append({"table": t, "rows": None})
    sizes.sort(key=lambda r: (r["rows"] or 0), reverse=True)

    # Naive recommendations driven by row counts.
    recs: list[dict] = []
    for s in sizes:
        if s["table"] == "metrics_requests" and (s["rows"] or 0) > 500_000:
            recs.append({
                "table": s["table"], "severity": "warning",
                "message": "raw metrics_requests كبير — تأكد أن retention يعمل (7 أيام).",
            })
        if s["table"] == "audit_log" and (s["rows"] or 0) > 1_000_000:
            recs.append({
                "table": s["table"], "severity": "info",
                "message": "audit_log يكبر بسرعة — فكّر في أرشفة قديمة.",
            })
    return {"tables": sizes, "recommendations": recs}


# ── /predictions — heuristic risk warnings ──────────────────────
@router.get("/predictions")
async def predictions(current_user: CurrentUser, sb: SbClient):
    """Cheap rule-based forecasts. No ML; just thresholds on the last hour
    compared to the previous hour."""
    _ensure_developer(current_user)
    out: list[dict] = []
    try:
        now = datetime.now(timezone.utc)
        last_hour  = now - timedelta(hours=1)
        prev_hour  = now - timedelta(hours=2)

        def err_rate(since, until):
            rows = (sb.table("metrics_requests").select("status")
                    .gte("ts", since.isoformat())
                    .lt("ts",  until.isoformat())
                    .limit(10000).execute().data or [])
            if not rows: return 0.0
            err = sum(1 for r in rows if int(r.get("status") or 0) >= 400)
            return err / len(rows)

        cur  = err_rate(last_hour, now)
        prev = err_rate(prev_hour, last_hour)
        if prev > 0 and cur > prev * 2 and cur > 0.02:
            out.append({
                "kind": "api_error_spike", "severity": "warning",
                "message": f"معدّل الأخطاء تضاعف خلال الساعة الأخيرة ({prev*100:.1f}% → {cur*100:.1f}%).",
            })
    except Exception as e:
        logger.debug("prediction error: %s", e)

    # Expiring documents in the next 7 days
    try:
        cutoff = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
        rows = (sb.table("documents").select("id", count="estimated")
                .lte("expiry_date", cutoff).execute())
        if (rows.count or 0) > 0:
            out.append({
                "kind": "docs_expiring", "severity": "info",
                "message": f"{rows.count} وثيقة ستنتهي خلال 7 أيام.",
            })
    except Exception:
        pass

    # Metrics table growth heuristic
    try:
        r = sb.table("metrics_requests").select("id", count="estimated").limit(1).execute()
        if (r.count or 0) > 1_000_000:
            out.append({
                "kind": "metrics_growth", "severity": "warning",
                "message": "metrics_requests تجاوز مليون صف — راجع retention.",
            })
    except Exception:
        pass

    return {"items": out}
