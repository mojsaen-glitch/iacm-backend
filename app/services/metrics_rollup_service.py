"""Background rollup + retention worker for the observability dashboard.

Per plan §10.1 we keep:
  •  raw  (metrics_requests)         7 days   — pruned hourly
  •  rollup-by-hour (..._h table)   90 days
  •  rollup-by-day  (..._d table)  365 days

Why three resolutions? A dashboard chart over "the last 30 days" must not
scan a million raw rows; reading 30×24 = 720 hourly rows is two orders of
magnitude cheaper. The day rollup keeps a year of trend data lean.

We run the worker in-process with APScheduler so the API + the rollup share
one Supabase connection pool. On Vercel, lifespan-managed workers persist
only across a single warm instance — that's fine here because the rollups
are idempotent and missing one cycle just delays a chart by an hour.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from supabase import Client

logger = logging.getLogger(__name__)

# Retention windows — tweak in one place if the plan changes.
RAW_RETENTION_DAYS    = 7
HOURLY_RETENTION_DAYS = 90
DAILY_RETENTION_DAYS  = 365


class MetricsRollupService:
    """Owns the APScheduler instance + the rollup/prune jobs."""

    _singleton: Optional["MetricsRollupService"] = None

    @classmethod
    def instance(cls) -> "MetricsRollupService":
        if cls._singleton is None:
            cls._singleton = cls()
        return cls._singleton

    def __init__(self) -> None:
        self._sched: Optional[AsyncIOScheduler] = None
        self._sb: Optional[Client] = None

    async def start(self, sb: Client) -> None:
        """Spin up the scheduler. Idempotent; safe to call on reload."""
        if self._sched is not None and self._sched.running:
            return
        self._sb = sb
        self._sched = AsyncIOScheduler(timezone="UTC")
        # Hourly rollup: aggregate the previous hour's raw rows. Run a few
        # minutes past the hour so any in-flight requests have been flushed.
        self._sched.add_job(self._rollup_hourly,
                            "cron", minute=5, id="rollup_hourly")
        # Daily rollup: roll up yesterday's hourly into the day table.
        self._sched.add_job(self._rollup_daily,
                            "cron", hour=1, minute=15, id="rollup_daily")
        # Retention pruner: delete raw rows older than 7 days, hourly older
        # than 90 days, daily older than 365.
        self._sched.add_job(self._prune_old_rows,
                            "cron", hour=2, minute=30, id="prune_old")
        # Alert engine — every minute, evaluate alert_rules vs live metrics.
        self._sched.add_job(self._run_alerts,
                            "interval", minutes=1, id="alert_engine",
                            max_instances=1, coalesce=True)
        self._sched.start()
        logger.info("MetricsRollupService started (hourly/daily/prune/alerts)")

    async def stop(self) -> None:
        if self._sched is not None and self._sched.running:
            self._sched.shutdown(wait=False)
            self._sched = None

    # ── jobs ────────────────────────────────────────────────────────────
    async def _rollup_hourly(self) -> None:
        """Aggregate raw requests from the previous full hour into ..._h.

        We compute count, error breakdowns, and approximate percentiles
        (p50/p95/p99) per (hour, path, method). Percentiles use
        `percentile_cont` via a Postgres function call — Supabase exposes
        it through `rpc()` if we register a SQL function, but for Phase 1
        we keep it simple and compute in Python from a single ordered
        fetch per (path, method) bucket — fine at our row volume.
        """
        if self._sb is None:
            return
        now = datetime.now(timezone.utc)
        # Previous full hour: [hour_start, hour_end).
        hour_end = now.replace(minute=0, second=0, microsecond=0)
        hour_start = hour_end - timedelta(hours=1)
        try:
            rows = (self._sb.table("metrics_requests")
                    .select("path,method,status,duration_ms")
                    .gte("ts", hour_start.isoformat())
                    .lt("ts",  hour_end.isoformat())
                    .limit(50000)
                    .execute().data or [])
        except Exception as e:
            logger.warning("hourly rollup fetch failed: %s", e)
            return
        if not rows:
            return
        # Bucket by (path, method).
        buckets: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            key = (r.get("path") or "", r.get("method") or "")
            buckets.setdefault(key, []).append(r)
        out: list[dict] = []
        for (path, method), items in buckets.items():
            durations = sorted(int(i.get("duration_ms") or 0) for i in items)
            n = len(durations)
            def pct(p: float) -> int:
                idx = max(0, min(n - 1, int(round(p * (n - 1)))))
                return durations[idx]
            errors_4xx = sum(1 for i in items if 400 <= int(i.get("status") or 0) < 500)
            errors_5xx = sum(1 for i in items if int(i.get("status") or 0) >= 500)
            out.append({
                "hour":       hour_start.isoformat(),
                "path":       path,
                "method":     method,
                "count":      n,
                "errors_4xx": errors_4xx,
                "errors_5xx": errors_5xx,
                "p50_ms":     pct(0.50),
                "p95_ms":     pct(0.95),
                "p99_ms":     pct(0.99),
                "avg_ms":     int(sum(durations) / n) if n else 0,
            })
        if out:
            try:
                self._sb.table("metrics_requests_h").upsert(
                    out, on_conflict="hour,path,method").execute()
                logger.info("hourly rollup: %d (path,method) buckets for %s",
                            len(out), hour_start.isoformat())
            except Exception as e:
                logger.warning("hourly rollup write failed: %s", e)

    async def _rollup_daily(self) -> None:
        """Aggregate the hourly table from the previous day into ..._d."""
        if self._sb is None:
            return
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)
        try:
            rows = (self._sb.table("metrics_requests_h")
                    .select("path,method,count,errors_4xx,errors_5xx,p95_ms,avg_ms")
                    .gte("hour", yesterday.isoformat())
                    .lt("hour",  today.isoformat())
                    .execute().data or [])
        except Exception as e:
            logger.warning("daily rollup fetch failed: %s", e)
            return
        buckets: dict[tuple[str, str], list[dict]] = {}
        for r in rows:
            key = (r.get("path") or "", r.get("method") or "")
            buckets.setdefault(key, []).append(r)
        out: list[dict] = []
        for (path, method), items in buckets.items():
            total = sum(int(i.get("count") or 0) for i in items)
            if total == 0:
                continue
            # Approximate p95 = max of hourly p95 (over-estimate, safer for
            # alerting); avg = weighted by count.
            p95 = max(int(i.get("p95_ms") or 0) for i in items)
            weighted_avg = sum(
                int(i.get("avg_ms") or 0) * int(i.get("count") or 0)
                for i in items) // total
            out.append({
                "day":        yesterday.date().isoformat(),
                "path":       path,
                "method":     method,
                "count":      total,
                "errors_4xx": sum(int(i.get("errors_4xx") or 0) for i in items),
                "errors_5xx": sum(int(i.get("errors_5xx") or 0) for i in items),
                "p95_ms":     p95,
                "avg_ms":     weighted_avg,
            })
        if out:
            try:
                self._sb.table("metrics_requests_d").upsert(
                    out, on_conflict="day,path,method").execute()
                logger.info("daily rollup: %d buckets for %s",
                            len(out), yesterday.date().isoformat())
            except Exception as e:
                logger.warning("daily rollup write failed: %s", e)

    async def _run_alerts(self) -> None:
        if self._sb is None:
            return
        try:
            from app.services.alert_engine import run_alert_engine_once
            fired = await run_alert_engine_once(self._sb)
            if fired:
                logger.info("alert engine: %d active alert(s)", fired)
        except Exception as e:
            logger.warning("alert engine error: %s", e)

    async def _prune_old_rows(self) -> None:
        """Delete rows past their retention window. Safe to run on every
        warm instance — DELETE WHERE on an indexed timestamp is cheap."""
        if self._sb is None:
            return
        now = datetime.now(timezone.utc)
        cutoffs = {
            "metrics_requests":   now - timedelta(days=RAW_RETENTION_DAYS),
            "metrics_requests_h": now - timedelta(days=HOURLY_RETENTION_DAYS),
            "metrics_requests_d": now - timedelta(days=DAILY_RETENTION_DAYS),
            "metrics_system":     now - timedelta(days=RAW_RETENTION_DAYS),
            "metrics_db":         now - timedelta(days=RAW_RETENTION_DAYS),
        }
        for table, cutoff in cutoffs.items():
            ts_col = "day" if table.endswith("_d") else ("hour" if table.endswith("_h") else "ts")
            try:
                self._sb.table(table).delete().lt(ts_col, cutoff.isoformat()).execute()
            except Exception as e:
                logger.warning("prune %s failed: %s", table, e)
