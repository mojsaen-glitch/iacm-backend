"""Crew Readiness Engine (Phase A) — advisory 0–100 score + status.

Weights: rest 25 · hours 25 · fdp 20 · documents_training 20 · qual/conflict 10.
Bands: 90–100 READY · 70–89 LIMITED · 50–69 FATIGUED · 0–49 BLOCKED.
Hard rule: ANY blocking issue → BLOCKED regardless of score.

Run:  py -m pytest tests/test_crew_readiness.py -q
"""
from app.core.compliance_engine import ComplianceEngine, Severity


def _eng():
    return ComplianceEngine(sb=None)  # readiness scoring needs no DB


def _issue(rule, severity):
    return {"rule": rule, "severity": severity,
            "is_blocking": severity == Severity.BLOCKING,
            "message_ar": f"{rule}", "message_en": rule}


def _readiness(issues):
    return _eng()._readiness_from_result({"status": "X", "issues": issues})


def test_clean_is_ready_100():
    r = _readiness([])
    assert r["readiness_score"] == 100
    assert r["readiness_status"] == "READY"
    assert r["readiness_color"] == "green"


def test_single_warning_is_limited():
    # One rest WARNING → rest factor 0.5 → 25*0.5 lost = 87.5 → round 88 → LIMITED.
    r = _readiness([_issue("rest_near_limit", Severity.WARNING)])
    assert r["readiness_status"] == "LIMITED"
    assert 70 <= r["readiness_score"] <= 89


def test_multiple_criticals_drive_fatigued():
    # 28-day CRITICAL (hours, factor .2 → lose 20) + docs unavailable CRITICAL
    # (factor .2 → lose 16) → 100-20-16 = 64 → FATIGUED.
    r = _readiness([
        _issue("hours_28day_exceeded", Severity.CRITICAL),
        _issue("check_documents_unavailable", Severity.CRITICAL),
    ])
    assert r["readiness_status"] == "FATIGUED"
    assert 50 <= r["readiness_score"] <= 69


def test_blocking_forces_blocked_even_if_score_high():
    # A single time-conflict (qual/conflict weight only 10) keeps score high,
    # but the BLOCKING severity must force BLOCKED.
    r = _readiness([_issue("assignment_conflict", Severity.BLOCKING)])
    assert r["readiness_status"] == "BLOCKED"
    assert r["readiness_color"] == "red"
    assert r["readiness_score"] >= 50  # proves it's the hard rule, not the score


def test_reasons_listed_and_capped():
    issues = [_issue(f"rest_x{i}", Severity.WARNING) for i in range(8)]
    r = _readiness(issues)
    assert len(r["readiness_reasons"]) <= 6
