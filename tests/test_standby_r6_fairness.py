"""Reserve/Standby — R6.2 (fairness metrics, read-only).

The standby report now carries a `fairness` block: per-crew response_rate,
distribution by base/rank/type, and ADVISORY imbalance flags
(over_standby / frequent_callout / low_reliability / under_covered_bases).
Read-only; changes no logic, feeds no algorithm, no payroll.

Run:  py -m pytest tests/test_standby_r6_fairness.py -q
"""
import asyncio
from datetime import datetime, timezone

from app.core.standby_report import compute_standby_report
from app.api.v1.endpoints.standby_report import standby_report

NOW = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
PAST = "2020-01-01T00:00:00+00:00"   # called_out_at far in the past → timed out


def _row(rid, crew, stype="HOME_STANDBY", **over):
    base = {"id": rid, "company_id": "c1", "crew_id": crew, "standby_type": stype,
            "status": "ACTIVE", "called_out": False, "called_out_at": None,
            "response_minutes": 60, "response_status": None, "assignment_id": None,
            "start_time": "2026-06-05T08:00:00+03:00",
            "end_time": "2026-06-05T18:00:00+03:00"}
    base.update(over)
    return base


def _imbalanced_rows():
    # cr1: 4 shifts, 3 callouts (1 accepted, 2 no-response) → over+frequent+low-rel
    # cr2: 1 shift (BGW), cr3: 1 shift (BSR) → BSR under-covered
    return [
        _row("a1", "cr1", "HOME_STANDBY", called_out=True, status="ASSIGNED",
             response_status="ACCEPTED", assignment_id="asg1",
             called_out_at="2026-06-05T08:30:00+03:00"),
        _row("a2", "cr1", "HOME_STANDBY", called_out=True, called_out_at=PAST),
        _row("a3", "cr1", "AIRPORT_STANDBY", called_out=True, called_out_at=PAST),
        _row("a4", "cr1", "AIRPORT_STANDBY"),
        _row("a5", "cr2", "READY_RESERVE"),
        _row("a6", "cr3", "HOME_STANDBY"),
    ]


def _crew():
    return {
        "cr1": {"id": "cr1", "full_name_ar": "علي", "rank": "captain", "base": "BGW"},
        "cr2": {"id": "cr2", "full_name_ar": "زيد", "rank": "first_officer", "base": "BGW"},
        "cr3": {"id": "cr3", "full_name_ar": "سعد", "rank": "captain", "base": "BSR"},
    }


# ── per-crew response_rate ───────────────────────────────────────────────────
def test_response_rate_per_crew():
    rep = compute_standby_report(_imbalanced_rows(), _crew(), NOW)
    by = {c["crew_id"]: c for c in rep["crew"]}
    assert by["cr1"]["response_rate"] == round(1 / 3, 3)   # 1 accepted of 3 callouts
    assert by["cr2"]["response_rate"] is None              # never called out


# ── distribution by base / rank / type ───────────────────────────────────────
def test_distribution_by_base_rank_type():
    f = compute_standby_report(_imbalanced_rows(), _crew(), NOW)["fairness"]
    assert f["distribution"]["by_base"]["BGW"]["shifts"] == 5   # cr1(4)+cr2(1)
    assert f["distribution"]["by_base"]["BSR"]["shifts"] == 1
    assert f["distribution"]["by_base"]["BGW"]["crew_count"] == 2
    assert f["distribution"]["by_rank"]["captain"]["shifts"] == 5   # cr1+cr3
    assert f["distribution"]["by_type"] == {
        "AIRPORT_STANDBY": 2, "HOME_STANDBY": 3, "READY_RESERVE": 1}


# ── imbalance flags ──────────────────────────────────────────────────────────
def test_outliers_flagged():
    f = compute_standby_report(_imbalanced_rows(), _crew(), NOW)["fairness"]
    o = f["outliers"]
    assert o["over_standby"] == ["cr1"]          # 4 shifts vs avg 2
    assert o["frequent_callout"] == ["cr1"]      # 3 callouts vs avg 1
    assert o["low_reliability"] == ["cr1"]       # callouts>=2 & rate<0.5
    assert o["under_covered_bases"] == ["BSR"]   # 1 shift vs avg base 3


def test_balanced_data_flags_nothing():
    rows = [_row("a1", "cr1"), _row("a2", "cr2")]
    crew = {"cr1": {"id": "cr1", "base": "BGW", "rank": "captain"},
            "cr2": {"id": "cr2", "base": "BGW", "rank": "captain"}}
    f = compute_standby_report(rows, crew, NOW)["fairness"]
    assert f["outliers"]["over_standby"] == []
    assert f["outliers"]["frequent_callout"] == []
    assert f["outliers"]["low_reliability"] == []
    assert f["outliers"]["under_covered_bases"] == []


def test_empty_fairness_is_clean():
    f = compute_standby_report([], {}, NOW)["fairness"]
    assert f["averages"]["shifts"] == 0
    assert f["distribution"]["by_base"] == {}
    assert f["outliers"]["over_standby"] == []


# ── endpoint surfaces fairness, still read-only ──────────────────────────────
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, store, name):
        self.store, self.name, self._filters = store, name, []

    def select(self, *a, **k): return self
    def eq(self, f, v): self._filters.append((f, v)); return self
    def in_(self, f, vals): self._filters.append((f, list(vals))); return self
    def gte(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, p): self.store.setdefault("_writes", []).append(self.name); return self
    def update(self, p): self.store.setdefault("_writes", []).append(self.name); return self

    def _match(self, r):
        for f, v in self._filters:
            if isinstance(v, list):
                if r.get(f) not in v:
                    return False
            elif r.get(f) != v:
                return False
        return True

    def execute(self):
        return _R([dict(r) for r in self.store.get(self.name, []) if self._match(r)])


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


ADMIN = {"id": "u1", "role": "admin", "company_id": "c1", "is_superuser": False}


def test_endpoint_includes_fairness_and_is_read_only():
    store = {"standby_assignments": _imbalanced_rows(),
             "crew": list(_crew().values())}
    res = asyncio.run(standby_report(
        current_user=ADMIN, sb=FakeSb(store), year=2026, month=6,
        base=None, rank=None, standby_type=None, status=None, company_id=None))
    assert "fairness" in res
    assert res["fairness"]["outliers"]["over_standby"] == ["cr1"]
    assert store.get("_writes") is None        # READ-ONLY
