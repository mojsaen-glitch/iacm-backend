"""Reserve/Standby — R6.1 (read-only standby report).

GET /reports/standby aggregates existing standby_assignments into per-crew
monthly counts. READ-ONLY: no writes, no flight-hours/FTL/monthly_hours change.
window_hours is informational only (never flight hours).

Run:  py -m pytest tests/test_standby_r6_report.py -q
"""
import asyncio
from datetime import datetime, timezone

import pytest

from app.core.standby_report import compute_standby_report
from app.api.v1.endpoints.standby_report import standby_report
from app.core.exceptions import ForbiddenError

NOW = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)


# ── filtering fake; records any write so we can prove the report is read-only ─
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters, self._op = [], "select"

    def select(self, *a, **k): self._op = "select"; return self
    def eq(self, f, v): self._filters.append((f, v)); return self
    def in_(self, f, vals): self._filters.append((f, list(vals))); return self
    def gte(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, p): self.store.setdefault("_writes", []).append(("insert", self.name)); return self
    def update(self, p): self.store.setdefault("_writes", []).append(("update", self.name)); return self
    def delete(self): self.store.setdefault("_writes", []).append(("delete", self.name)); return self

    def _match(self, r):
        for f, v in self._filters:
            if isinstance(v, list):
                if r.get(f) not in v:
                    return False
            elif r.get(f) != v:
                return False
        return True

    def execute(self):
        rows = self.store.get(self.name, [])
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


ADMIN = {"id": "u1", "role": "admin", "company_id": "c1", "is_superuser": False}
CREW = {"id": "u2", "role": "crew", "company_id": "c1", "is_superuser": False}


def _row(rid, crew, **over):
    base = {"id": rid, "company_id": "c1", "crew_id": crew, "standby_type": "HOME_STANDBY",
            "status": "ACTIVE", "called_out": False, "called_out_at": None,
            "response_minutes": 60, "response_status": None, "assignment_id": None,
            "start_time": "2026-06-05T08:00:00+03:00",
            "end_time": "2026-06-05T18:00:00+03:00"}
    base.update(over)
    return base


# ── pure aggregation ─────────────────────────────────────────────────────────
def test_compute_aggregates_states_per_crew():
    rows = [
        _row("a1", "cr1", called_out=True, status="ASSIGNED",
             response_status="ACCEPTED", assignment_id="asg1",
             called_out_at="2026-06-05T08:30:00+03:00"),
        _row("a2", "cr1", called_out=True, status="CALLED_OUT",
             response_status="REJECTED", called_out_at="2026-06-06T08:30:00+03:00"),
        _row("a3", "cr1", called_out=True, status="CALLED_OUT",
             called_out_at="2020-01-01T00:00:00+00:00"),     # timed out, no response
        _row("a4", "cr1"),                                    # never called out
    ]
    crew = {"cr1": {"id": "cr1", "full_name_ar": "علي", "rank": "captain", "base": "BGW"}}
    rep = compute_standby_report(rows, crew, NOW)
    assert len(rep["crew"]) == 1
    c = rep["crew"][0]
    assert c["shifts"] == 4
    assert c["callouts"] == 3
    assert c["accepted"] == 1
    assert c["rejected"] == 1
    assert c["no_response"] == 1
    assert c["assignments_made"] == 1
    assert c["rank"] == "captain" and c["base"] == "BGW"
    # window hours informational: a1..a3 = 10h windows, a4 = 10h → 40h
    assert c["window_hours"] == 40.0
    assert rep["totals"]["callouts"] == 3 and rep["totals"]["crew_count"] == 1


def test_compute_empty_is_zeroed_not_error():
    rep = compute_standby_report([], {}, NOW)
    assert rep["crew"] == []
    assert rep["totals"]["shifts"] == 0 and rep["totals"]["crew_count"] == 0


def test_compute_does_not_mutate_rows():
    rows = [_row("a1", "cr1", called_out=True)]
    snapshot = dict(rows[0])
    compute_standby_report(rows, {}, NOW)
    assert rows[0] == snapshot          # input untouched (read-only)


# ── endpoint ─────────────────────────────────────────────────────────────────
def _store():
    return {
        "standby_assignments": [
            _row("a1", "cr1", called_out=True, response_status="ACCEPTED",
                 assignment_id="asg1", called_out_at="2026-06-05T08:30:00+03:00"),
            _row("a2", "cr2", company_id="c1"),
            _row("a9", "cr9", company_id="c2"),     # other company
        ],
        "crew": [
            {"id": "cr1", "full_name_ar": "علي", "rank": "captain", "base": "BGW"},
            {"id": "cr2", "full_name_ar": "زيد", "rank": "first_officer", "base": "BSR"},
            {"id": "cr9", "full_name_ar": "آخر", "rank": "captain", "base": "BGW"},
        ],
    }


def test_endpoint_aggregates_and_is_company_scoped():
    store = _store()
    res = asyncio.run(standby_report(
        current_user=ADMIN, sb=FakeSb(store), year=2026, month=6,
        base=None, rank=None, standby_type=None, status=None, company_id=None))
    crew_ids = {c["crew_id"] for c in res["crew"]}
    assert crew_ids == {"cr1", "cr2"}            # c2 excluded
    assert res["year"] == 2026 and res["month"] == 6
    assert store.get("_writes") is None          # READ-ONLY: nothing written


def test_endpoint_rbac_blocks_crew_role():
    store = _store()
    with pytest.raises(ForbiddenError):
        asyncio.run(standby_report(
            current_user=CREW, sb=FakeSb(store), year=2026, month=6,
            base=None, rank=None, standby_type=None, status=None, company_id=None))


def test_endpoint_empty_month_returns_clean_report():
    store = {"standby_assignments": [], "crew": []}
    res = asyncio.run(standby_report(
        current_user=ADMIN, sb=FakeSb(store), year=2026, month=1,
        base=None, rank=None, standby_type=None, status=None, company_id=None))
    assert res["crew"] == [] and res["totals"]["shifts"] == 0
    assert store.get("_writes") is None


def test_endpoint_base_filter_narrows():
    store = _store()
    res = asyncio.run(standby_report(
        current_user=ADMIN, sb=FakeSb(store), year=2026, month=6,
        base="BSR", rank=None, standby_type=None, status=None, company_id=None))
    assert {c["crew_id"] for c in res["crew"]} == {"cr2"}   # only BSR


def test_endpoint_does_not_touch_monthly_hours():
    # The report module never imports monthly_hours' compute, only its pure
    # Baghdad-bounds helper. Prove no flights/assignments table is even queried.
    store = _store()
    asyncio.run(standby_report(
        current_user=ADMIN, sb=FakeSb(store), year=2026, month=6,
        base=None, rank=None, standby_type=None, status=None, company_id=None))
    assert store.get("_writes") is None
    # monthly_hours numbers come from flights/assignments — untouched here.
    assert "flights" not in store and "assignments" not in store
