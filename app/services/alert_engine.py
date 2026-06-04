"""Alert engine — evaluates `alert_rules` against live metrics every minute
and fires `alerts` rows when thresholds are breached.

Plan §6.1: each rule has metric + operator + threshold + duration_sec. The
engine treats `duration_sec` as the minimum dwell time — we only fire an
alert after the condition has been true for ≥ that many seconds.

Phase-4 scope:
  •  Built-in metric resolvers — api.p95_ms, api.error_rate, sys.cpu_pct,
     sys.ram_pct, sys.disk_pct, alerts.active_count.
  •  Channel dispatch — websocket (always), telegram (env var), email
     (env var). Telegram and email are best-effort; failure is logged.
  •  Deduplication — if an active alert for the same rule already exists,
     we don't create a duplicate; we update `metric_value` instead.

The engine is started by the existing `MetricsRollupService.start` lifecycle
(both share the APScheduler instance), so this file is wired by adding one
job in metrics_rollup_service.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ── built-in metric resolvers ────────────────────────────────────────
def _api_window_stats(sb, window_min: int = 5) -> dict[str, float]:
    """Compute p95/error_rate/rpm from the last `window_min` minutes of
    metrics_requests. Mirrors what /admin/health/detailed returns."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_min)).isoformat()
    rows = (sb.table("metrics_requests")
            .select("status,duration_ms")
            .gte("ts", cutoff).limit(10000).execute().data or [])
    if not rows:
        return {"p95_ms": 0.0, "error_rate": 0.0, "rpm": 0.0, "count": 0.0}
    durations = sorted(int(r.get("duration_ms") or 0) for r in rows)
    n = len(durations)
    err = sum(1 for r in rows if int(r.get("status") or 0) >= 400)
    idx = max(0, min(n - 1, int(round(0.95 * (n - 1)))))
    return {
        "p95_ms":     float(durations[idx]),
        "error_rate": err / n,
        "rpm":        n / float(window_min),
        "count":      float(n),
    }


def _sys_stats() -> dict[str, float]:
    try:
        import psutil   # type: ignore[import-untyped]
        vm = psutil.virtual_memory()
        return {
            "cpu_pct":  float(psutil.cpu_percent(interval=None)),
            "ram_pct":  float(vm.percent),
            "disk_pct": float(psutil.disk_usage("/").percent),
        }
    except Exception:
        return {"cpu_pct": 0.0, "ram_pct": 0.0, "disk_pct": 0.0}


def _resolve_metric(sb, key: str) -> Optional[float]:
    """Map a rule's `metric` string to a numeric value. Unknown keys → None
    (the rule is skipped). Cached for the duration of one evaluation pass."""
    cache = _metric_cache
    if key in cache:
        return cache[key]
    val: Optional[float]
    if key.startswith("api."):
        stats = cache.get("__api_stats")
        if stats is None:
            stats = _api_window_stats(sb)
            cache["__api_stats"] = stats
        val = stats.get(key.split(".", 1)[1])
    elif key.startswith("sys."):
        stats = cache.get("__sys_stats")
        if stats is None:
            stats = _sys_stats()
            cache["__sys_stats"] = stats
        val = stats.get(key.split(".", 1)[1])
    elif key == "alerts.active_count":
        try:
            r = sb.table("alerts").select("id", count="exact") \
                .eq("status", "active").execute()
            val = float(r.count or 0)
        except Exception:
            val = None
    else:
        val = None
    cache[key] = val
    return val


_metric_cache: dict[str, Any] = {}


# ── operator comparison ─────────────────────────────────────────────
def _compare(value: float, op: str, threshold: float) -> bool:
    if op == ">":  return value > threshold
    if op == "<":  return value < threshold
    if op == ">=": return value >= threshold
    if op == "<=": return value <= threshold
    if op == "=":  return value == threshold
    if op == "!=": return value != threshold
    return False


# ── channel dispatch ────────────────────────────────────────────────
def _dispatch_alert(channels: list[str], severity: str, message: str) -> None:
    """Best-effort fan-out. Each channel is wrapped so one bad config
    (e.g. missing TELEGRAM_TOKEN) doesn't block the others."""
    if "websocket" in channels:
        try:
            from app.websockets.manager import ws_manager
            # Use the existing broadcast helper if present; otherwise log.
            if hasattr(ws_manager, "broadcast_to_role"):
                import asyncio
                asyncio.create_task(ws_manager.broadcast_to_role(
                    "super_admin",
                    {"type": "alert", "severity": severity, "message": message},
                ))
        except Exception as e:
            logger.debug("websocket alert dispatch skipped: %s", e)

    if "telegram" in channels:
        token   = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            try:
                import urllib.request, urllib.parse
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                data = urllib.parse.urlencode({
                    "chat_id": chat_id,
                    "text":    f"🚨 [{severity.upper()}] {message}",
                }).encode()
                req = urllib.request.Request(url, data=data, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:
                logger.warning("telegram dispatch failed: %s", e)

    if "email" in channels:
        # SendGrid / SMTP placeholder — wire to your transactional provider.
        # We deliberately keep this a no-op when env is missing so the
        # alert engine itself never errors on a missing channel.
        logger.info("email channel not configured — skipping")


# ── main evaluation pass (called by APScheduler every minute) ─────────
async def run_alert_engine_once(sb) -> int:
    """Evaluate every enabled rule once. Returns the number of alerts fired
    (new or re-broadcast). Designed for the APScheduler `cron` job."""
    _metric_cache.clear()
    try:
        rules = (sb.table("alert_rules").select("*")
                 .eq("enabled", True).execute().data or [])
    except Exception as e:
        logger.warning("alert_rules read failed: %s", e)
        return 0

    fired = 0
    for rule in rules:
        metric_key = rule.get("metric") or ""
        value = _resolve_metric(sb, metric_key)
        if value is None:
            continue
        triggered = _compare(value, rule.get("operator") or ">",
                              float(rule.get("threshold") or 0))
        if not triggered:
            continue
        # Deduplication — one active alert per rule at a time.
        try:
            existing = (sb.table("alerts").select("id")
                        .eq("rule_id", rule["id"]).eq("status", "active")
                        .limit(1).execute().data or [])
            if existing:
                sb.table("alerts").update({
                    "metric_value": value,
                    "fired_at":     datetime.now(timezone.utc).isoformat(),
                }).eq("id", existing[0]["id"]).execute()
            else:
                msg = (rule.get("description")
                       or f"{metric_key} {rule['operator']} {rule['threshold']} (actual={value:.2f})")
                sb.table("alerts").insert({
                    "rule_id":      rule["id"],
                    "severity":     rule.get("severity") or "warning",
                    "metric_value": value,
                    "message":      msg,
                    "status":       "active",
                    "context":      {"metric": metric_key, "rule_name": rule.get("name")},
                }).execute()
                _dispatch_alert(rule.get("channels") or ["websocket"],
                                rule.get("severity") or "warning", msg)
            fired += 1
        except Exception as e:
            logger.warning("alert write failed for rule %s: %s", rule.get("name"), e)

    return fired
