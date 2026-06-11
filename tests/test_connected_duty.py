"""ComplianceEngine.check_connected_duty — multi-sector single-duty checks.

Uses an in-memory fake Supabase so it runs with no network. Proves: a same-day
round trip passes as ONE duty (no rest between sectors), the turnaround gap isn't
treated as rest, a too-long gap is rejected, FDP/training block, warnings don't,
and a bound OM clause stamps its number on the violation.

Run:  py -m pytest tests/test_connected_duty.py -q
"""
from datetime import datetime, timezone

from app.core.compliance_engine import ComplianceEngine, ComplianceStatus


# ── In-memory fake Supabase ────────────────────────────────────────────────
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
    def __init__(self, tables): self._t = tables
    def table(self, name): return _Query(self._t.get(name, []))


def _crew(**o):
    b = {"id": "c1", "company_id": "co1", "status": "active",
         "full_name_ar": "طيار", "full_name_en": "Pilot", "max_monthly_hours": 100.0}
    b.update(o); return b


def _flight(fid, dep, arr, origin, dest, **o):
    b = {"id": fid, "flight_number": "IA" + fid, "status": "scheduled",
         "departure_time": dep, "arrival_time": arr,
         "origin_code": origin, "destination_code": dest, "aircraft_type": ""}
    b.update(o); return b


def _fdp_rule(max_min):
    return {"acclimatisation_state": "acclimated",
            "start_band_from": "00:00:00", "start_band_to": "23:59:59",
            "sectors_from": 1, "sectors_to": 99,
            "max_fdp_minutes": max_min, "is_frm": False}


# A same-day round trip BGW→JED→BGW with a 1h turnaround at JED.
def _round_trip():
    return [
        _flight("OUT", "2026-05-26T06:00:00Z", "2026-05-26T08:00:00Z", "BGW", "JED"),
        _flight("RET", "2026-05-26T09:00:00Z", "2026-05-26T11:00:00Z", "JED", "BGW"),
    ]


def _engine(flights, *, fdp_max=780, training=None, om=None, crew=None):
    tables = {
        "crew": [crew or _crew()],
        "flights": flights,
        "assignments": [],
        "documents": [],
        "training_records": training or [],
        "fdp_rules": [_fdp_rule(fdp_max)],
        "om_articles": om or [],
    }
    return ComplianceEngine(FakeSb(tables))


IDS = ["OUT", "RET"]


# 1 + 2. Round trip = one duty; the 1h turnaround is NOT rest.
def test_round_trip_passes_as_one_duty():
    res = _engine(_round_trip()).check_connected_duty("c1", IDS)
    rules = [i["rule"] for i in res["issues"]]
    assert "rest_insufficient" not in rules
    assert "connected_duty_gap_too_long" not in rules
    assert res["status"] != ComplianceStatus.BLOCKED
    assert res["duty"]["sectors"] == 2
    assert res["duty"]["turnarounds"][0]["minutes"] == 60


# 3. A gap beyond MAX_TURNAROUND_HOURS is a separate duty → rejected.
def test_long_gap_rejected():
    flights = [
        _flight("OUT", "2026-05-26T06:00:00Z", "2026-05-26T08:00:00Z", "BGW", "JED"),
        _flight("RET", "2026-05-26T13:00:00Z", "2026-05-26T15:00:00Z", "JED", "BGW"),  # 5h gap
    ]
    res = _engine(flights).check_connected_duty("c1", IDS)
    assert res["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"] == "connected_duty_gap_too_long" for i in res["issues"])


# 4. FDP over the whole duty exceeded → blocks.
def test_fdp_exceeded_blocks():
    res = _engine(_round_trip(), fdp_max=60).check_connected_duty("c1", IDS)
    assert res["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"] == "fdp_exceeded" for i in res["issues"])


# 5. Expired training → blocks.
def test_expired_training_blocks():
    res = _engine(_round_trip(),
                  training=[{"crew_id": "c1", "training_type": "recurrent",
                             "expiry_date": "2020-01-01"}]).check_connected_duty("c1", IDS)
    assert res["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"].startswith("training_expired") for i in res["issues"])


# 6. A warning (near-limit FDP) does NOT block.
def test_warning_does_not_block():
    # Duty FDP = 05:00→11:30 = 390 min. max 410 → 390 ≥ 0.9*410 (=369) and < 410.
    res = _engine(_round_trip(), fdp_max=410).check_connected_duty("c1", IDS)
    rules = [i["rule"] for i in res["issues"]]
    assert "fdp_near_limit" in rules
    assert res["status"] != ComplianceStatus.BLOCKED


# 7. A bound OM clause stamps its number on the violation message.
def test_om_clause_number_on_violation():
    om = [{"id": "OM-C 7.2", "bound_check_key": "fdp", "rule_type": "blocking",
           "is_active": True, "affects_compliance": True}]
    res = _engine(_round_trip(), fdp_max=60, om=om).check_connected_duty("c1", IDS)
    fdp = [i for i in res["issues"] if i["rule"] == "fdp_exceeded"][0]
    assert fdp["om_ref"] == "OM-C 7.2"
    assert fdp["message_ar"].startswith("OM-C 7.2:")


# 8. FDC #3: batch_connected_duty must produce the SAME result as
# check_connected_duty for every crew (no behavioural divergence), in a fraction
# of the queries. Two crew: one clean, one with expired training.
def test_batch_matches_single_per_crew():
    tables = {
        "crew": [_crew(id="c1", full_name_en="One"), _crew(id="c2", full_name_en="Two")],
        "flights": _round_trip(),
        "assignments": [],
        "documents": [],
        "training_records": [{"crew_id": "c2", "training_type": "recurrent",
                              "expiry_date": "2020-01-01"}],
        "fdp_rules": [_fdp_rule(780)],
        "om_articles": [],
    }
    batch = {r["crew_id"]: r
             for r in ComplianceEngine(FakeSb(tables)).batch_connected_duty(["c1", "c2"], IDS)}
    for cid in ("c1", "c2"):
        single = ComplianceEngine(FakeSb(tables)).check_connected_duty(cid, IDS)
        b = batch[cid]
        assert b["status"] == single["status"], cid
        assert sorted(i["rule"] for i in b["issues"]) == \
               sorted(i["rule"] for i in single["issues"]), cid
        assert sorted(b["blocking_reasons"]) == sorted(single["blocking_reasons"]), cid
        assert b["duty"]["sectors"] == single["duty"]["sectors"]
    # The clean crew passes; the expired-training crew is blocked.
    assert batch["c1"]["status"] != ComplianceStatus.BLOCKED
    assert batch["c2"]["status"] == ComplianceStatus.BLOCKED
    assert any(i["rule"].startswith("training_expired") for i in batch["c2"]["issues"])
