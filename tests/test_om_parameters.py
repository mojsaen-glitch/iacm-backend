"""Phase B — the Compliance Engine reads live values from OM `parameters`.

Proves the clause's operational values (not its text, not only config) drive the
verdict: changing max_hours / max_turnaround_hours / rest hours changes the
outcome; missing params fall back to config; an inactive clause changes nothing;
a warning clause warns instead of blocking.

Run:  py -m pytest tests/test_om_parameters.py -q
"""
from datetime import date, datetime, timezone

from app.core.compliance_engine import ComplianceEngine, ComplianceStatus


class _Resp:
    def __init__(self, data): self.data = data


class _Query:
    def __init__(self, rows): self._rows = list(rows)
    def select(self, *_a, **_k): return self
    def eq(self, c, v): self._rows = [r for r in self._rows if r.get(c) == v]; return self
    def in_(self, c, vals):
        s = set(vals); self._rows = [r for r in self._rows if r.get(c) in s]; return self
    def order(self, *_a, **_k): return self
    def execute(self): return _Resp(list(self._rows))


class FakeSb:
    def __init__(self, t): self._t = t
    def table(self, name): return _Query(self._t.get(name, []))


def _crew(**o):
    b = {"id": "c1", "company_id": "co1", "status": "active",
         "full_name_ar": "طيار", "full_name_en": "Pilot", "max_monthly_hours": 100.0}
    b.update(o); return b


def _flight(fid, dep, arr, origin, dest, **o):
    b = {"id": fid, "flight_number": "IA" + fid, "status": "scheduled",
         "departure_time": dep, "arrival_time": arr,
         "origin_code": origin, "destination_code": dest,
         "aircraft_type": "", "duration_hours": 0}
    b.update(o); return b


def _om(check_key, **over):
    b = {"id": "OM-X", "bound_check_key": check_key, "rule_type": "blocking",
         "is_active": True, "affects_compliance": True, "parameters": {}}
    b.update(over); return b


def _fdp_rule(maxm=780):
    return {"acclimatisation_state": "acclimated",
            "start_band_from": "00:00:00", "start_band_to": "23:59:59",
            "sectors_from": 1, "sectors_to": 99, "max_fdp_minutes": maxm, "is_frm": False}


# ── 2. Changing max_hours in parameters changes the block ──────────────────
def _hours_engine(om):
    today = date.today().isoformat()
    return ComplianceEngine(FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "F1"}],
        "flights": [_flight("F1", f"{today}T06:00:00Z", f"{today}T09:00:00Z",
                            "BGW", "DXB", duration_hours=60)],
        "documents": [], "training_records": [], "fdp_rules": [_fdp_rule()],
        "om_articles": om,
    }))


def test_max_hours_param_changes_block():
    # 60 yearly hours: legal under config (900) but breaches a clause max_hours=50.
    no_om = _hours_engine([]).check_crew(crew_id="c1")
    assert not any(i["rule"] == "hours_yearly_exceeded" for i in no_om["issues"])

    with_om = _hours_engine([_om("flight_hours_yearly", parameters={"max_hours": 50})]) \
        .check_crew(crew_id="c1")
    assert with_om["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"] == "hours_yearly_exceeded" for i in with_om["issues"])


# ── 6. Inactive clause changes nothing (params ignored → config applies) ───
def test_inactive_clause_does_not_change_law():
    res = _hours_engine([_om("flight_hours_yearly", is_active=False,
                             parameters={"max_hours": 50})]).check_crew(crew_id="c1")
    assert res["status"] != ComplianceStatus.BLOCKED
    assert not any(i["rule"] == "hours_yearly_exceeded" for i in res["issues"])


# ── 7. Warning clause warns instead of blocking (param still triggers) ─────
def test_warning_clause_warns_not_blocks():
    res = _hours_engine([_om("flight_hours_yearly", rule_type="warning",
                             parameters={"max_hours": 50})]).check_crew(crew_id="c1")
    yearly = [i for i in res["issues"] if i["rule"] == "hours_yearly_exceeded"]
    assert yearly and yearly[0]["severity"] == "WARNING"
    assert res["status"] != ComplianceStatus.BLOCKED


# ── 3 + 4. Rest hours param changes the rest verdict; missing → fallback ───
def _rest_engine(om):
    return ComplianceEngine(FakeSb({
        "crew": [_crew()],
        "assignments": [{"crew_id": "c1", "flight_id": "A"}],
        "flights": [
            _flight("A", "2026-05-26T00:00:00Z", "2026-05-26T02:00:00Z", "BGW", "CAI"),
            _flight("B", "2026-05-26T07:00:00Z", "2026-05-26T09:00:00Z", "JED", "BGW"),
        ],
        "fdp_rules": [_fdp_rule()], "om_articles": om,
    }))


def _rest_issues(eng):
    return eng._check_rest("c1", datetime(2026, 5, 26, 7, 0, tzinfo=timezone.utc),
                           is_international=False, flight_id="B")


def test_rest_param_changes_verdict():
    # 5h gap, different station. Config (10h) blocks; a clause min=4h allows.
    blocked = _rest_issues(_rest_engine([]))
    assert any(i.rule == "rest_insufficient" for i in blocked)

    allowed = _rest_issues(_rest_engine(
        [_om("rest", parameters={"domestic_min_rest_hours": 4})]))
    assert not any(i.rule == "rest_insufficient" for i in allowed)


def test_missing_params_fall_back_to_config():
    # Clause is bound + active but carries NO values → config (10h) still applies.
    issues = _rest_issues(_rest_engine([_om("rest", parameters={})]))
    assert any(i.rule == "rest_insufficient" for i in issues)


# ── 4b. max_turnaround_hours changes connected-duty accept/reject ──────────
def _ct_engine(om):
    return ComplianceEngine(FakeSb({
        "crew": [_crew()], "assignments": [],
        "flights": [
            _flight("OUT", "2026-05-26T01:00:00Z", "2026-05-26T03:00:00Z", "BGW", "JED"),
            _flight("RET", "2026-05-26T08:00:00Z", "2026-05-26T10:00:00Z", "JED", "BGW"),  # 5h gap
        ],
        "documents": [], "training_records": [], "fdp_rules": [_fdp_rule()],
        "om_articles": om,
    }))


def test_turnaround_param_changes_connected_duty():
    ids = ["OUT", "RET"]
    # Default ceiling 3h → 5h gap rejected.
    default = _ct_engine([]).check_connected_duty("c1", ids)
    assert any(i["rule"] == "connected_duty_gap_too_long" for i in default["issues"])
    # Clause raises ceiling to 6h → 5h gap accepted as one duty.
    raised = _ct_engine([_om("turnaround", parameters={"max_turnaround_hours": 6})]) \
        .check_connected_duty("c1", ids)
    assert not any(i["rule"] == "connected_duty_gap_too_long" for i in raised["issues"])


# ── Validation: out-of-range numbers are rejected client→server (pnum guard) ─
def test_invalid_param_falls_back_safely():
    # max_hours = 0 is nonsense; engine ignores it and uses config (no false block).
    res = _hours_engine([_om("flight_hours_yearly", parameters={"max_hours": 0})]) \
        .check_crew(crew_id="c1")
    assert not any(i["rule"] == "hours_yearly_exceeded" for i in res["issues"])
