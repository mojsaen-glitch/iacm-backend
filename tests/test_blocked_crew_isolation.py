"""/compliance/blocked-crew must isolate per-crew failures.

Proves:
  • One crew record that makes the engine raise (missing data / invalid time)
    does NOT 500 the whole board — it is surfaced as BLOCKED with a clear,
    localized reason and the loop keeps checking the rest.
  • A total DB failure (can't even fetch the crew list) still raises (→ 500).

Run:  venv/Scripts/python -m pytest tests/test_blocked_crew_isolation.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.compliance_engine import ComplianceEngine, ComplianceStatus, Severity
from app.api.v1.endpoints.compliance import get_blocked_crew


_CREW = [
    {"id": "good", "full_name_ar": "طاقم سليم", "full_name_en": "Good", "rank": "captain"},
    {"id": "bad",  "full_name_ar": "طاقم بيانات ناقصة", "full_name_en": "Bad", "rank": "senior"},
    {"id": "warn", "full_name_ar": "طاقم تحذير", "full_name_en": "Warn", "rank": "cabin_crew"},
]


# ── Fake Supabase: crew fetch succeeds ─────────────────────────────────────
class _OkQ:
    def __init__(self, data): self._data = data
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): return SimpleNamespace(data=self._data)


class OkSb:
    def __init__(self, crew): self._crew = crew
    def table(self, _name): return _OkQ(self._crew)


# ── Fake Supabase: crew fetch itself blows up ──────────────────────────────
class _RaisingQ:
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): raise RuntimeError("db down")


class RaisingSb:
    def table(self, _name): return _RaisingQ()


_USER = {"company_id": "co1", "id": "u1", "role": "ops_manager"}


def _fake_check(self, crew_id):
    """good → GREEN (excluded), warn → YELLOW (included), bad → raises."""
    if crew_id == "bad":
        raise ValueError("invalid duty time: naive vs aware compare")
    status = ComplianceStatus.GREEN if crew_id == "good" else ComplianceStatus.YELLOW
    return {
        "crew_id": crew_id, "crew_name_ar": "", "crew_name_en": "",
        "employee_id": "", "rank": "", "status": status, "issues": [],
        "blocking_count": 0, "critical_count": 0, "warning_count": 0,
        "info_count": 0, "blocking_reasons": [], "checked_at": "",
    }


def test_one_bad_record_does_not_500(monkeypatch):
    monkeypatch.setattr(ComplianceEngine, "check_crew", _fake_check)
    out = asyncio.run(get_blocked_crew(current_user=_USER, sb=OkSb(_CREW), status_filter=None))

    # Endpoint returned a list (200), did not raise.
    assert isinstance(out, list)
    by_id = {r["crew_id"]: r for r in out}

    # GREEN crew excluded; YELLOW + the failed-check crew included.
    assert "good" not in by_id
    assert "warn" in by_id
    assert "bad" in by_id


def test_failed_record_surfaced_with_clear_reason(monkeypatch):
    monkeypatch.setattr(ComplianceEngine, "check_crew", _fake_check)
    out = asyncio.run(get_blocked_crew(current_user=_USER, sb=OkSb(_CREW), status_filter=None))
    bad = next(r for r in out if r["crew_id"] == "bad")

    assert bad["status"] == ComplianceStatus.BLOCKED
    assert bad["issues"][0]["rule"] == "compliance_check_error"
    assert bad["issues"][0]["severity"] == Severity.BLOCKING
    assert "تعذر فحص الامتثال" in bad["issues"][0]["message_ar"]
    assert bad["blocking_reasons"], "must carry a human-readable reason"


def test_status_filter_still_applies(monkeypatch):
    monkeypatch.setattr(ComplianceEngine, "check_crew", _fake_check)
    out = asyncio.run(
        get_blocked_crew(current_user=_USER, sb=OkSb(_CREW), status_filter="BLOCKED")
    )
    # Only the failed-check (BLOCKED) crew matches the filter.
    assert [r["crew_id"] for r in out] == ["bad"]


def test_total_db_failure_still_raises():
    # No crew list at all → cannot reason about compliance → must propagate (500).
    with pytest.raises(Exception):
        asyncio.run(get_blocked_crew(current_user=_USER, sb=RaisingSb(), status_filter=None))
