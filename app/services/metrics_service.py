"""Observability metrics — buffered per-request capture + background flush.

Phase-1 implementation of the dashboard plan (§4.2 step 4-5): the middleware
puts every request into an asyncio queue with zero await-on-DB cost. A
background task drains the queue every 10 seconds and writes a batch to
`metrics_requests` in Supabase. If Supabase is unreachable the batch is
silently dropped (the buffer is bounded so we never balloon memory).

Why an in-process buffer instead of writing on every request?
  •  Each Supabase insert is ~50-150ms of network — adding it to every
     request would defeat the point of the dashboard ("must add <5ms" per
     plan §3.2 NFRs).
  •  Batched inserts amortise the connection cost across hundreds of rows.
  •  If the dashboard storage is down, the main API keeps serving.

Concurrency model:
  •  `asyncio.Queue` is the single producer/consumer boundary. Producers
     are request handlers; consumer is a single background task started on
     FastAPI startup.
  •  `Queue.put_nowait` never blocks — if the queue is full we drop the
     event and increment a counter; the dashboard surfaces drop rate.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

logger = logging.getLogger(__name__)

# Bounded queue — if the consumer falls behind (e.g. Supabase is down) we
# stop accepting new events instead of OOMing. 10k slots ≈ 1MB of events.
_MAX_QUEUE = 10_000
_FLUSH_INTERVAL_SEC = 10.0
_FLUSH_BATCH_SIZE = 500            # Supabase row limit per insert request


class MetricsCollector:
    """Singleton owning the in-process metrics queue + background flusher.

    Use ``MetricsCollector.instance()`` from middleware to enqueue; call
    ``await collector.start(sb)`` once at FastAPI startup to spin up the
    flush loop, and ``await collector.stop()`` on shutdown.
    """

    _singleton: Optional["MetricsCollector"] = None

    @classmethod
    def instance(cls) -> "MetricsCollector":
        if cls._singleton is None:
            cls._singleton = cls()
        return cls._singleton

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._task: Optional[asyncio.Task] = None
        self._sb: Optional[Client] = None
        self.dropped = 0          # surfaced in /admin/health/detailed
        self.flushed = 0
        self.last_flush_at: Optional[datetime] = None
        self.last_flush_error: Optional[str] = None

    # ── producer side (called from middleware) ──────────────────────────
    def record(self, *, method: str, path: str, status: int, duration_ms: int,
               user_id: Optional[str] = None, company_id: Optional[str] = None,
               role: Optional[str] = None, ip: Optional[str] = None,
               user_agent: Optional[str] = None,
               request_id: Optional[str] = None) -> None:
        """Push a single request event. Never raises, never blocks."""
        event = {
            "id":          str(uuid.uuid4()),
            "ts":          datetime.now(timezone.utc).isoformat(),
            "method":      method,
            "path":        path,
            "status":      int(status),
            "duration_ms": int(duration_ms),
            "user_id":     user_id,
            "company_id":  company_id,
            "role":        role,
            "ip":          ip,
            "user_agent":  (user_agent or "")[:255] or None,
            "request_id":  request_id,
        }
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1     # consumer behind — visible on the dashboard

    # ── consumer side (background task) ────────────────────────────────
    async def start(self, sb: Client) -> None:
        """Start the background flusher. Idempotent — calling twice is safe."""
        if self._task is not None and not self._task.done():
            return
        self._sb = sb
        self._task = asyncio.create_task(self._flush_loop(), name="metrics_flush_loop")
        logger.info("MetricsCollector started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        await self._flush_once()  # one last drain
        self._task = None

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_FLUSH_INTERVAL_SEC)
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never let the flush loop die — log and keep going.
                logger.exception("metrics flush loop error: %s", e)

    async def _flush_once(self) -> None:
        if self._sb is None or self._queue.empty():
            return
        batch: list[dict] = []
        # drain up to _FLUSH_BATCH_SIZE in one go
        while len(batch) < _FLUSH_BATCH_SIZE:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        try:
            # Supabase insert is sync (HTTP) — run in a thread so the event
            # loop stays free for the next request burst.
            await asyncio.to_thread(
                lambda: self._sb.table("metrics_requests").insert(batch).execute()
            )
            self.flushed += len(batch)
            self.last_flush_at = datetime.now(timezone.utc)
            self.last_flush_error = None
        except Exception as e:
            self.last_flush_error = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning("metrics flush failed (%d events lost): %s",
                           len(batch), self.last_flush_error)

    # ── snapshot for /admin/health/detailed ────────────────────────────
    def snapshot(self) -> dict:
        return {
            "queue_depth":      self._queue.qsize(),
            "queue_capacity":   _MAX_QUEUE,
            "flushed_total":    self.flushed,
            "dropped_total":    self.dropped,
            "last_flush_at":    self.last_flush_at.isoformat() if self.last_flush_at else None,
            "last_flush_error": self.last_flush_error,
        }
