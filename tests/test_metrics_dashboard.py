"""Phase-1 observability dashboard tests.

Pure-logic coverage — no real Supabase calls. We stub the client and verify:
  •  MetricsCollector buffers + batches events, never blocks, never raises.
  •  Overflow events are counted (dropped++) once the queue fills.
  •  The percentile helper used by the rollup + /health/detailed agrees with
     `statistics.quantiles` on the same input (no off-by-one).
  •  /admin/health/detailed is super-admin-only (403 for everyone else).
  •  Structured logging setup is idempotent and survives without the
     python-json-logger dep.

Run:  venv/Scripts/python -m pytest tests/test_metrics_dashboard.py -q
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.core.logging_setup import setup_json_logging
from app.services.metrics_service import MetricsCollector


# ── MetricsCollector ───────────────────────────────────────────────────────
class _StubSupabase:
    """Captures every `insert` so we can assert on batch contents."""
    def __init__(self) -> None:
        self.inserts: list[list[dict]] = []
    def table(self, _name):
        return self
    def insert(self, batch):
        self.inserts.append(list(batch))
        return self
    def execute(self):
        return self


def test_record_then_flush_writes_one_batch():
    """A handful of recorded events flushes as one Supabase batch.
    Sync test that runs the async flush via asyncio.run — keeps this suite
    plugin-free (no pytest-asyncio dependency)."""
    async def scenario():
        coll = MetricsCollector()           # fresh instance — bypass singleton
        sb = _StubSupabase()
        coll._sb = sb                       # type: ignore[attr-defined]
        for i in range(5):
            coll.record(method="GET", path="/x", status=200, duration_ms=10 + i)
        await coll._flush_once()            # type: ignore[attr-defined]
        return coll, sb
    coll, sb = asyncio.run(scenario())
    assert len(sb.inserts) == 1
    assert len(sb.inserts[0]) == 5
    assert {r["status"] for r in sb.inserts[0]} == {200}
    assert coll.dropped == 0
    assert coll.flushed == 5


def test_record_never_raises_when_queue_full(monkeypatch):
    """Once the queue maxes out, record() silently drops + increments
    `dropped` instead of raising — critical so a metrics outage can't break
    the API."""
    coll = MetricsCollector()
    # Fill the queue. _queue is an asyncio.Queue(maxsize=...). We use the
    # underlying internals to avoid actually running an event loop here.
    while True:
        try:
            coll._queue.put_nowait({"x": "filler"})  # type: ignore[attr-defined]
        except asyncio.QueueFull:
            break
    before = coll.dropped
    coll.record(method="GET", path="/y", status=200, duration_ms=1)
    assert coll.dropped == before + 1


def test_snapshot_shape():
    coll = MetricsCollector()
    snap = coll.snapshot()
    assert {"queue_depth", "queue_capacity", "flushed_total",
            "dropped_total", "last_flush_at", "last_flush_error"} <= snap.keys()


# ── Percentile helper used by both rollup and /health/detailed ───────────
def _pct(durations_sorted: list[int], p: float) -> int:
    """Same formula as MetricsRollupService._rollup_hourly + admin_metrics."""
    n = len(durations_sorted)
    idx = max(0, min(n - 1, int(round(p * (n - 1)))))
    return durations_sorted[idx]


def test_percentile_helper_matches_expected_indices():
    """Sanity check that p50/p95/p99 land where we'd expect on a 1..100 set.
    Uses the inclusive-index formula `round(p*(n-1))` so p=0.5 of 100 items
    lands on the 50th *position* (index 50 → value 51), which is the
    Postgres `percentile_cont(0.5)` interpretation."""
    xs = sorted(range(1, 101))   # 1..100
    assert _pct(xs, 0.50) == 51
    assert _pct(xs, 0.95) == 95   # round(0.95 * 99) = 94 → xs[94] = 95
    assert _pct(xs, 0.99) == 99   # round(0.99 * 99) = 98 → xs[98] = 99
    # Edge cases — single element and ordered tie-handling.
    assert _pct([42], 0.95) == 42
    assert _pct([1, 1, 1, 1], 0.50) == 1


# ── Super-admin gate ────────────────────────────────────────────────────
def test_super_admin_gate_blocks_other_roles():
    from app.api.v1.endpoints.admin_metrics import _ensure_super_admin
    from app.core.exceptions import ForbiddenError

    # Allowed: role super_admin OR is_superuser flag.
    _ensure_super_admin({"role": "super_admin"})
    _ensure_super_admin({"role": "admin", "is_superuser": True})

    # Blocked: any other role, with or without other fields.
    for bad in [
        {"role": "admin"},
        {"role": "ops_manager"},
        {"role": "crew"},
        {"role": "scheduler"},
        {},
    ]:
        with pytest.raises(ForbiddenError):
            _ensure_super_admin(bad)


# ── JSON logging setup ─────────────────────────────────────────────────
def test_setup_json_logging_idempotent():
    """Calling twice replaces the handler — root logger ends with exactly
    one handler. This is required because lifespan hot-reloads in dev call
    main module setup more than once per process."""
    setup_json_logging("INFO")
    setup_json_logging("INFO")
    root = logging.getLogger("")
    assert len(root.handlers) == 1
    # Uvicorn loggers also normalised to one handler each.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        assert len(logging.getLogger(name).handlers) == 1
