"""Shared test fixtures.

The company-settings loader keeps a 60s in-process cache; tests reuse company
ids across files, so the cache is cleared around EVERY test to keep them
hermetic (a customized value in one test must never leak into the next)."""
import pytest

from app.core.company_settings import invalidate_settings_cache


@pytest.fixture(autouse=True)
def _fresh_company_settings_cache():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()
