"""Reserve/Standby — R6.3 (monthly roster DRAFT preview).

Generates a PROPOSED standby roster and returns it — persists nothing, creates
no standby/assignment/callout, never activates. Eligibility reuses R4; fairness
reuses the R6.2 load. Uncovered slots come back with reasons.

Run:  py -m pytest tests/test_standby_r6_roster.py -q
"""
import asyncio

import pytest

import app.api.v1.endpoints.standby as standby_mod
from app.core.standby_roster import generate_standby_roster_draft
from app.api.v1.endpoints.standby_report import standby_roster_draft
from app.core.exceptions import ForbiddenError
from fastapi import HTTPException


def _pool(*ids, base="BGW", rank="captain"):
    return [{"id": i, "base": base, "rank": rank, "name_ar": i, "name_en": i}
            for i in ids]


REQ = [{"base": "BGW", "rank": "captain", "per_day": 1}]


# ── pure generator ───────────────────────────────────────────────────────────
def test_fills_one_per_day_and_spreads_fairly():
    ok = lambda c, s, e: ([], [])      # everyone eligible
    d = generate_standby_roster_draft(
        year=2026, month=6, requirements=REQ, crew_pool=_pool("cr1", "cr2"),
        base_load={}, is_eligible=ok)
    assert d["summary"]["slots_filled"] == 30 and d["summary"]["uncovered"] == 0
    counts = {}
    for s in d["slots"]:
        counts[s["crew_id"]] = counts.get(s["crew_id"], 0) + 1
        assert s["status"] == "DRAFT"
    # fairness: the two captains share the month within one shift of each other
    assert abs(counts["cr1"] - counts["cr2"]) <= 1


def test_ineligible_crew_is_skipped():
    # cr1 always blocked; cr2 eligible → every slot goes to cr2.
    def elig(c, s, e):
        return (["تعارض زمني"], []) if c == "cr1" else ([], [])
    d = generate_standby_roster_draft(
        year=2026, month=6, requirements=REQ, crew_pool=_pool("cr1", "cr2"),
        base_load={}, is_eligible=elig)
    assert d["summary"]["slots_filled"] == 30 and d["summary"]["uncovered"] == 0
    assert all(s["crew_id"] == "cr2" for s in d["slots"])


def test_all_blocked_yields_uncovered_with_reasons():
    def elig(c, s, e):
        return (["وثيقة منتهية"], [])
    d = generate_standby_roster_draft(
        year=2026, month=6, requirements=REQ, crew_pool=_pool("cr1"),
        base_load={}, is_eligible=elig)
    assert d["summary"]["slots_filled"] == 0 and d["summary"]["uncovered"] == 30
    u = d["uncovered"][0]
    assert u["reason_category"] == "no_eligible_candidate"
    assert "وثيقة منتهية" in u["reasons"]


def test_empty_pool_for_base_rank():
    ok = lambda c, s, e: ([], [])
    d = generate_standby_roster_draft(
        year=2026, month=6,
        requirements=[{"base": "XXX", "rank": "captain", "per_day": 1}],
        crew_pool=_pool("cr1"), base_load={}, is_eligible=ok)
    assert d["summary"]["slots_filled"] == 0 and d["summary"]["uncovered"] == 30
    assert d["uncovered"][0]["reason_category"] == "no_crew_in_base_rank"


def test_one_per_person_per_day_leaves_second_slot_uncovered():
    # per_day=2 but only cr1 eligible (cr2 blocked) → slot1=cr1, slot2 uncovered.
    def elig(c, s, e):
        return ([], []) if c == "cr1" else (["تعارض"], [])
    d = generate_standby_roster_draft(
        year=2026, month=6,
        requirements=[{"base": "BGW", "rank": "captain", "per_day": 2}],
        crew_pool=_pool("cr1", "cr2"), base_load={}, is_eligible=elig)
    assert d["summary"]["slots_filled"] == 30      # one cr1 per day
    assert d["summary"]["uncovered"] == 30         # second slot each day
    assert all(s["crew_id"] == "cr1" for s in d["slots"])


def test_existing_load_steers_fairness():
    # cr1 already heavily loaded → cr2 (load 0) is picked first each day.
    ok = lambda c, s, e: ([], [])
    d = generate_standby_roster_draft(
        year=2026, month=6, requirements=REQ, crew_pool=_pool("cr1", "cr2"),
        base_load={"cr1": 100}, is_eligible=ok)
    counts = {}
    for s in d["slots"]:
        counts[s["crew_id"]] = counts.get(s["crew_id"], 0) + 1
    assert counts.get("cr2", 0) > counts.get("cr1", 0)   # the idle one fills more


# ── endpoint (read-only preview) ─────────────────────────────────────────────
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
CREW = {"id": "u2", "role": "crew", "company_id": "c1", "is_superuser": False}


def _store():
    return {
        "crew": [
            {"id": "cr1", "company_id": "c1", "full_name_ar": "علي",
             "rank": "captain", "base": "BGW"},
            {"id": "cr2", "company_id": "c1", "full_name_ar": "زيد",
             "rank": "captain", "base": "BGW"},
            {"id": "cr9", "company_id": "c2", "full_name_ar": "آخر",
             "rank": "captain", "base": "BGW"},     # other company
        ],
        "standby_assignments": [],
    }


def test_endpoint_previews_draft_read_only(monkeypatch):
    monkeypatch.setattr(standby_mod, "_standby_eligibility",
                        lambda sb, c, s, e: ([], []))
    store = _store()
    res = asyncio.run(standby_roster_draft(
        {"year": 2026, "month": 6, "requirements": REQ},
        current_user=ADMIN, sb=FakeSb(store)))
    assert res["summary"]["slots_filled"] == 30
    assert all(s["status"] == "DRAFT" for s in res["slots"])
    # company scope: the c2 captain is never proposed
    assert all(s["crew_id"] in {"cr1", "cr2"} for s in res["slots"])
    # READ-ONLY: nothing written, no assignments created
    assert store.get("_writes") is None
    assert "assignments" not in store


def test_endpoint_rbac_blocks_crew(monkeypatch):
    monkeypatch.setattr(standby_mod, "_standby_eligibility",
                        lambda sb, c, s, e: ([], []))
    with pytest.raises(ForbiddenError):
        asyncio.run(standby_roster_draft(
            {"year": 2026, "month": 6, "requirements": REQ},
            current_user=CREW, sb=FakeSb(_store())))


def test_endpoint_requires_requirements():
    with pytest.raises(HTTPException) as ei:
        asyncio.run(standby_roster_draft(
            {"year": 2026, "month": 6, "requirements": []},
            current_user=ADMIN, sb=FakeSb(_store())))
    assert ei.value.status_code == 422


def test_endpoint_uses_r4_eligibility(monkeypatch):
    # Prove the SAME R4 gate is what filters: block cr1, only cr2 is proposed.
    def elig(sb, c, s, e):
        return (["تعارض زمني"], []) if c == "cr1" else ([], [])
    monkeypatch.setattr(standby_mod, "_standby_eligibility", elig)
    res = asyncio.run(standby_roster_draft(
        {"year": 2026, "month": 6, "requirements": REQ},
        current_user=ADMIN, sb=FakeSb(_store())))
    assert all(s["crew_id"] == "cr2" for s in res["slots"])
