"""Phase 0 — monitoring wiring.

The sentry-test endpoint is the end-to-end alert check: it raises a REAL
unhandled error (so Sentry captures it when SENTRY_DSN is configured), and it
is super_admin-gated so it can never be triggered anonymously.

Run:  py -m pytest tests/test_phase0_monitoring.py -q
"""
import asyncio

import pytest

from app.core.exceptions import ForbiddenError
from app.main import sentry_test


def test_sentry_test_requires_super_admin():
    with pytest.raises(ForbiddenError):
        asyncio.run(sentry_test(current_user={"id": "u1", "role": "ops_manager",
                                              "is_superuser": False}))


def test_sentry_test_raises_deliberate_error_for_super_admin():
    with pytest.raises(RuntimeError):
        asyncio.run(sentry_test(current_user={"id": "u0", "role": "super_admin",
                                              "is_superuser": True}))


def test_sentry_disabled_without_dsn():
    """No DSN ⇒ init never ran ⇒ no Sentry client attached (CI/local safe)."""
    from app.core.config import settings
    if settings.SENTRY_DSN:
        pytest.skip("DSN configured in this environment")
    import sentry_sdk
    assert sentry_sdk.get_client().is_active() is False
