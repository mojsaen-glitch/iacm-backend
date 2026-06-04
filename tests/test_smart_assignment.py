"""#3 Smart Weighted Assignment — scoring + ranking of suggestion candidates.

Weights: readiness 40% · fewest monthly hours 20% · rested 20% ·
least projected FDP 10% · qualification 10%.

Run:  py -m pytest tests/test_smart_assignment.py -q
"""
from app.api.v1.endpoints.assignments import _assignment_score, _rank_candidates


def _cand(name, score, comp="GREEN", ready="READY"):
    return {"name": name, "assignment_score": score,
            "compliance_status": comp, "readiness_status": ready}


def test_low_hours_not_always_first_when_readiness_weak():
    # A: very low hours (20h) but weak readiness (55). B: more hours (70h) but
    # strong readiness (95). The smart score should rank B above A.
    a = _assignment_score(readiness_score=55, monthly=20, max_monthly=100,
                          rested=True, fdp_min=None, qualified=True)
    b = _assignment_score(readiness_score=95, monthly=70, max_monthly=100,
                          rested=True, fdp_min=None, qualified=True)
    assert b > a, "high-readiness crew must outrank low-hours-but-weak-readiness"


def test_ready_outranks_limited_all_else_equal():
    ready   = _assignment_score(95, 40, 100, True, None, True)
    limited = _assignment_score(75, 40, 100, True, None, True)
    assert ready > limited


def test_rested_beats_resting():
    rested  = _assignment_score(90, 40, 100, True, None, True)
    resting = _assignment_score(90, 40, 100, False, None, True)
    assert rested > resting


def test_qualified_beats_unqualified():
    q  = _assignment_score(90, 40, 100, True, None, True)
    nq = _assignment_score(90, 40, 100, True, None, False)
    assert q > nq


def test_blocked_always_last_even_with_high_score():
    cands = [
        _cand("blocked_high", 99, comp="BLOCKED", ready="BLOCKED"),
        _cand("ready_mid", 70),
        _cand("ready_low", 40),
    ]
    ranked = _rank_candidates(cands)
    assert ranked[-1]["name"] == "blocked_high"
    assert ranked[0]["name"] == "ready_mid"           # highest non-blocked first
    assert [r["assignment_rank"] for r in ranked] == [1, 2, 3]


def test_higher_score_ranks_first():
    cands = [_cand("low", 50), _cand("high", 88), _cand("mid", 70)]
    ranked = _rank_candidates(cands)
    assert [r["name"] for r in ranked] == ["high", "mid", "low"]
