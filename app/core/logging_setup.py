"""Structured JSON logging — emit every log line as a single-line JSON object
that ships cleanly to a log collector (and to the dashboard's log viewer in
Phase 3) without regex parsing.

Plan §10.1 calls for "JSON Structured Logging" as Phase-1 infrastructure.
We replace the default uvicorn formatters so application + access logs are
homogeneous, and add a small `extra` adapter so handlers can attach
request_id / user_id / route fields without touching the formatter.

Usage in app code:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("crew assigned", extra={"crew_id": cid, "flight_id": fid})

The two extra keys appear as top-level JSON fields, ready for indexing.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

try:
    # The package is `python-json-logger`; the importable name is `pythonjsonlogger`.
    from pythonjsonlogger import jsonlogger
except Exception:  # graceful degradation: keep plain-text logs if not installed
    jsonlogger = None  # type: ignore[assignment]


_DEFAULT_FIELDS = (
    "asctime", "levelname", "name", "message",
    "module", "funcName", "lineno",
    # Application-specific context (populated via `extra=`):
    "request_id", "user_id", "route", "status", "duration_ms",
)


def setup_json_logging(level: Optional[str] = None) -> None:
    """Install the JSON formatter on the root logger + uvicorn loggers.

    Idempotent — calling twice replaces the previous handler. Honours the
    ``LOG_LEVEL`` env var (default INFO) and falls back silently to plain
    text if python-json-logger isn't installed (so dev installs without the
    optional dep still work).
    """
    lvl_name = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, lvl_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    if jsonlogger is not None:
        fmt = jsonlogger.JsonFormatter(
            "%(" + ")s %(".join(_DEFAULT_FIELDS) + ")s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        handler.setFormatter(fmt)
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    # Replace handlers on the root + uvicorn — both write to stdout already
    # but with their own (incompatible) formats. We want one JSON shape.
    for logger_name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(logger_name)
        lg.handlers = [handler]
        lg.setLevel(lvl)
        lg.propagate = False
