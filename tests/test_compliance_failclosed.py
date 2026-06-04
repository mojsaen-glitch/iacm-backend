"""Safety-critical scheduling fixes — fail-CLOSED engine + override scope.

Proves:
  • When a safety check (conflict / rest / FDP) errors internally, the engine
    returns a BLOCKING `compliance_engine_error` — it NEVER silently passes.
  • The override classifier only treats FTL/FDP/rest as bypassable; conflict,
    crew-blocked, qualification and engine-errors stay HARD.

Run:  py -m pytest tests/test_compliance_failclosed.py -q
"""
from datetime import datetime, timezone, timedelta

from app.core.compliance_engine import ComplianceEngine, Severity
from app.api.v1.endpoints.assignments import _is_overridable_block


# ── Fake Supabase whose every query raises on execute ──────────────────────
class _RaisingQ:
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def execute(self): raise RuntimeError("db down")


class RaisingSb:
    def table(self, _name): return _RaisingQ()


_DEP = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
_ARR = _DEP + timedelta(hours=2)


def _engine():
    return ComplianceEngine(RaisingSb())


def test_conflict_check_fails_closed():
    issues = _engine()._check_conflict("c1", "f1", _DEP, _ARR)
    assert len(issues) == 1
    assert issues[0].rule == "compliance_engine_error"
    assert issues[0].severity == Severity.BLOCKING


def test_rest_check_fails_closed():
    issues = _engine()._check_rest("c1", _DEP, True, "f1")
    assert any(i.rule == "compliance_engine_error" and i.is_blocking for i in issues)


def test_fdp_check_fails_closed():
    issues = _engine()._check_fdp("c1", "f1", _DEP, _ARR)
    assert any(i.rule == "compliance_engine_error" and i.is_blocking for i in issues)


def test_engine_error_is_blocking_and_unmanaged_by_om():
    # The fail-closed error must NOT bind to any OM family (so OM can't disable it).
    assert ComplianceEngine._binding_key("compliance_engine_error") is None


# ── Override scope: only FTL/FDP/rest is bypassable ────────────────────────
def test_overridable_only_for_ftl_family():
    assert _is_overridable_block("rest_insufficient") is True
    assert _is_overridable_block("fdp_exceeded") is True
    assert _is_overridable_block("hours_monthly_exceeded") is True


def test_hard_blocks_never_overridable():
    for rule in (
        "assignment_conflict",
        "crew_status_blocked",
        "aircraft_not_type_rated",
        "doc_expired_license",
        "training_expired_safety",
        "compliance_engine_error",
        "connected_duty_overlap",
    ):
        assert _is_overridable_block(rule) is False, rule
