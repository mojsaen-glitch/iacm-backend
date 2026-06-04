"""Capture every request's timing + outcome for the observability dashboard.

Plan §4.2 step 2-4: a thin ASGI middleware that times each request and pushes
the result into the in-process `MetricsCollector` queue. The queue's flusher
batches inserts to Supabase every 10 seconds (see metrics_service.py).

Design constraints:
  •  MUST NOT add measurable latency. We do a single monotonic clock pair +
     one queue put — both O(1), both non-blocking.
  •  MUST NOT break the response on any failure. The whole record block is
     wrapped in a broad except so a logging bug never bubbles to the user.
  •  Route normalisation: we use `request.scope['route'].path` (the template
     like '/api/v1/crew/{id}') instead of the raw URL — otherwise a high-
     cardinality endpoint with UUIDs explodes the metrics table.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.services.metrics_service import MetricsCollector

logger = logging.getLogger(__name__)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Times each request and pushes one metric event per response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Correlation id — emitted in JSON logs + returned to client so they
        # can quote it when reporting an issue.
        req_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = req_id

        start = time.perf_counter()
        status_code = 500          # default; overwritten on success
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = req_id
            return response
        finally:
            try:
                duration_ms = int((time.perf_counter() - start) * 1000)
                # Use the matched route template if FastAPI resolved one —
                # otherwise fall back to the raw path (404s, OPTIONS, etc).
                route = request.scope.get("route")
                path = getattr(route, "path", None) or request.url.path
                # Pull auth context if it's already been resolved by deps
                # (avoid re-running auth here just for metrics).
                user = getattr(request.state, "current_user", None) or {}
                MetricsCollector.instance().record(
                    method=request.method,
                    path=path,
                    status=status_code,
                    duration_ms=duration_ms,
                    user_id=user.get("id"),
                    company_id=user.get("company_id"),
                    role=user.get("role"),
                    ip=_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    request_id=req_id,
                )
            except Exception:
                # NEVER let a metrics failure affect the response.
                logger.exception("metrics middleware suppressed error")


def _client_ip(request: Request) -> str | None:
    """Honour X-Forwarded-For when behind Vercel/Railway proxy, else fall
    back to the direct peer. We only keep the leftmost (originating) IP."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else None
