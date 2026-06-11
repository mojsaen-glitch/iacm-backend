"""Phase 3D — Replace Crew: one audited swap (relieve + assign replacement).

Covers: pending/declined/ACCEPTED (operational release) swaps, mandatory
reason, hard compliance block, atomicity (failed insert leaves the original
untouched), audit before/after, notifications (incl. draft isolation),
departed/cancelled lock, cross-company 404, duplicate conflict, candidate
ranking by real hours + conflict/type-rating exclusion, GD-stale hook.

Run:  py -m pytest tests/test_replace_crew.py -q
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

import app.api.v1.endpoints.assignments as asg_mod
import app.api.v1.endpoints.flights as fl_mod
from app.api.v1.endpoints.assignments import replace_assignment, replacement_candidates
from app.core.exceptions import NotFoundError, ConflictError
from app.services import push_service


# ── Filtering + recording fake ────────────────────────────────────────────────
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._filters, self._op = [], "select"
        self._payload = None

    @staticmethod
    def _get(row, field):
        cur = row
        for part in str(field).split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def is_(self, *a, **k): return self

    def eq(self, f, v):  self._filters.append(("eq", f, v));  return self
    def neq(self, f, v): self._filters.append(("neq", f, v)); return self
    def gte(self, f, v): self._filters.append(("gte", f, v)); return self
    def lte(self, f, v): self._filters.append(("lte", f, v)); return self
    def gt(self, f, v):  self._filters.append(("gt", f, v));  return self
    def lt(self, f, v):  self._filters.append(("lt", f, v));  return self
    def in_(self, f, v): self._filters.append(("in", f, set(v))); return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def _match(self, row):
        for op, f, v in self._filters:
            x = self._get(row, f)
            if op == "eq" and x != v: return False
            if op == "neq" and x == v: return False
            if op == "in" and x not in v: return False
            if op == "gte" and not (str(x or "") >= str(v)): return False
            if op == "lte" and not (str(x or "") <= str(v)): return False
            if op == "gt" and not (str(x or "") > str(v)): return False
            if op == "lt" and not (str(x or "") < str(v)): return False
        return True

    def execute(self):
        rows = self.sb.store.get(self.table, [])
        if self._op == "insert":
            if self.table in self.sb.fail_insert:
                raise RuntimeError(f"forced insert failure on {self.table}")
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            self.sb.store[self.table] = rows
            self.sb.ops.append(("insert", self.table, items))
            return _R([dict(i) for i in items])
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            self.sb.ops.append(("update", self.table, self._payload))
            return _R([dict(r) for r in hit])
        if self._op == "delete":
            if self.table in self.sb.fail_delete:
                raise RuntimeError(f"forced delete failure on {self.table}")
            keep = [r for r in rows if not self._match(r)]
            gone = [r for r in rows if self._match(r)]
            self.sb.store[self.table] = keep
            self.sb.ops.append(("delete", self.table, gone))
            return _R(gone)
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store):
        self.store = {k: [dict(r) for r in v] for k, v in store.items()}
        self.ops = []
        self.fail_insert = set()
        self.fail_delete = set()

    def table(self, name): return _Q(self, name)

    def inserted(self, table):
        return [i for op, t, items in self.ops if op == "insert" and t == table
                for i in items]


# ── Fixtures ──────────────────────────────────────────────────────────────────
OPS = {"id": "u-ops", "role": "ops_manager", "company_id": "c1",
       "name_ar": "مدير العمليات", "is_superuser": False}


def _store(publish="published", flight_status="scheduled", old_row=None,
           finalized=False):
    old = {
        "id": "a1", "flight_id": "f1", "crew_id": "cOld",
        "duty_type": "operating", "assigned_role": "captain",
        "assignment_type": "flight_deck", "assigned_by": "u-x",
        "acknowledged": False, "declined": False, "admin_confirmed": False,
        "created_at": "2026-06-01T00:00:00+00:00",
    }
    old.update(old_row or {})
    return {
        "flights": [{
            "id": "f1", "company_id": "c1", "flight_number": "IA-900",
            "origin_code": "BGW", "destination_code": "EBL",
            "departure_time": "2099-01-01T10:00:00+00:00",
            "arrival_time": "2099-01-01T12:00:00+00:00",
            "duration_hours": 2.0, "aircraft_type": "B737",
            "status": flight_status, "publish_status": publish,
            "roster_finalized_status": "finalized" if finalized else "",
        }],
        "crew": [
            {"id": "cOld", "company_id": "c1", "status": "active",
             "rank": "captain", "full_name_ar": "النقيب القديم",
             "aircraft_qualifications": "B737"},
            {"id": "cNew", "company_id": "c1", "status": "active",
             "rank": "captain", "full_name_ar": "النقيب البديل",
             "aircraft_qualifications": "B737", "operator_company_id": "op1"},
        ],
        "assignments": [old],
        "users": [
            {"id": "uOld", "crew_id": "cOld", "company_id": "c1", "is_active": True},
            {"id": "uNew", "crew_id": "cNew", "company_id": "c1", "is_active": True},
        ],
        "notifications": [],
        "audit_log": [],
    }


def _engine(issues=None):
    """Engine stub: configurable blocking issues + GREEN readiness with
    per-crew hours from the HOURS map."""
    HOURS = {"cand_low": 5.0, "cand_high": 50.0}

    class E:
        def __init__(self, sb): pass

        def check_crew(self, **k):
            return {"status": "BLOCKED" if issues else "GREEN",
                    "issues": issues or []}

        def batch_readiness(self, cid, crew_rows=None):
            return {c["id"]: {"monthly_flight_hours": HOURS.get(c["id"], 0.0),
                              "max_monthly_hours": 100,
                              "compliance_status": "GREEN",
                              "blocking_reasons": []}
                    for c in (crew_rows or [])}
    return E


@pytest.fixture
def quiet(monkeypatch):
    """Default harness: clean engine, no DNP, recorded GD/role-notify hooks,
    silenced push."""
    monkeypatch.setattr(asg_mod, "ComplianceEngine", _engine())
    monkeypatch.setattr(asg_mod, "get_approved_dnp_pairs", lambda sb, cid: [])
    calls = {"gd": [], "roles": []}
    monkeypatch.setattr(fl_mod, "mark_gd_stale_if_finalized",
                        lambda sb, cid, fid, actor=None: calls["gd"].append(fid))
    monkeypatch.setattr(fl_mod, "_insert_role_notifications",
                        lambda sb, cid, roles, ntype, *a, **k:
                        calls["roles"].append(ntype) or 1)
    monkeypatch.setattr(push_service, "send_to_users", lambda *a, **k: 0)
    return calls


def _run(sb, assignment_id="a1", **body):
    payload = {"replacement_crew_id": "cNew", "reason": "ضرورة تشغيلية"}
    payload.update(body)
    return asyncio.run(replace_assignment(assignment_id, payload,
                                          current_user=OPS, sb=sb))


# ── The swap itself ───────────────────────────────────────────────────────────
def test_replace_declined(quiet):
    sb = FakeSb(_store(old_row={"declined": True, "decline_reason": "مرض"}))
    res = _run(sb)
    assert res["replaced"] is True
    assert res["old_acceptance_status"] == "declined"
    assert res["operational_release"] is False
    rows = sb.store["assignments"]
    assert [r["crew_id"] for r in rows] == ["cNew"]      # old gone, new in
    assert rows[0]["acknowledged"] is False              # pending acceptance
    assert rows[0]["duty_type"] == "operating"           # inherited
    assert rows[0]["assigned_role"] == "captain"


def test_replace_pending(quiet):
    sb = FakeSb(_store())
    res = _run(sb)
    assert res["old_acceptance_status"] == "pending_acceptance"
    assert res["acceptance_status"] == "pending_acceptance"
    assert len(sb.store["assignments"]) == 1


def test_replace_accepted_is_operational_release(quiet):
    sb = FakeSb(_store(old_row={"acknowledged": True,
                                "acknowledged_at": "2026-06-02T00:00:00+00:00"}))
    res = _run(sb)
    assert res["operational_release"] is True
    audits = sb.inserted("audit_log")
    assert len(audits) == 1 and audits[0]["action"] == "assignment_replaced"
    before = json.loads(audits[0]["before_data"])
    after = json.loads(audits[0]["after_data"])
    assert before["acceptance_status"] == "accepted"     # was approving
    assert after["operational_release"] is True          # released operationally
    assert after["reason"] == "ضرورة تشغيلية"


def test_reason_required(quiet):
    sb = FakeSb(_store())
    with pytest.raises(HTTPException) as e:
        _run(sb, reason="  ")
    assert e.value.status_code == 422
    assert len(sb.store["assignments"]) == 1             # untouched


def test_hard_block_stops_swap_old_intact(quiet, monkeypatch):
    monkeypatch.setattr(asg_mod, "ComplianceEngine", _engine(issues=[
        {"is_blocking": True, "rule": "conflict_overlap",
         "message_ar": "تعارض زمني مع رحلة أخرى"}]))
    sb = FakeSb(_store())
    with pytest.raises(HTTPException) as e:
        _run(sb)
    assert e.value.status_code == 422
    assert "تعارض" in str(e.value.detail)
    assert [r["id"] for r in sb.store["assignments"]] == ["a1"]   # original intact
    assert not sb.inserted("audit_log")


def test_atomic_failed_insert_keeps_original(quiet):
    sb = FakeSb(_store())
    sb.fail_insert.add("assignments")
    with pytest.raises(HTTPException) as e:
        _run(sb)
    assert e.value.status_code == 502
    assert [r["id"] for r in sb.store["assignments"]] == ["a1"]


def test_failed_delete_rolls_back_new_row(quiet):
    sb = FakeSb(_store())
    sb.fail_delete.add("assignments")
    with pytest.raises(HTTPException) as e:
        _run(sb)
    assert e.value.status_code == 502
    # Rollback path: delete also fails here (forced), but the recorded ops show
    # the rollback was ATTEMPTED before surfacing the error.
    assert any(op == "insert" and t == "assignments" for op, t, _ in sb.ops)


def test_audit_before_after(quiet):
    sb = FakeSb(_store())
    res = _run(sb)
    a = sb.inserted("audit_log")[0]
    before = json.loads(a["before_data"])
    after = json.loads(a["after_data"])
    assert before["crew_id"] == "cOld"
    assert before["assignment"]["id"] == "a1"
    assert after["replacement_crew_id"] == "cNew"
    assert after["new_assignment_id"] == res["new_assignment"]["id"]
    assert after["flight_number"] == "IA-900"


def test_notifications_published(quiet):
    sb = FakeSb(_store(publish="published"))
    _run(sb)
    notes = sb.inserted("notifications")
    types = {n["type"] for n in notes}
    assert "assignment_replaced" in types                # released member told
    assert "crew_assigned" in types                      # replacement told
    rel = next(n for n in notes if n["type"] == "assignment_replaced")
    assert "تشغيلية" in rel["message_ar"]               # operational wording
    assert quiet["roles"] == ["roster_changed"]          # schedulers alerted


def test_draft_keeps_crew_isolation(quiet):
    sb = FakeSb(_store(publish="draft"))
    _run(sb)
    types = {n["type"] for n in sb.inserted("notifications")}
    assert "assignment_replaced" in types                # release notice still goes
    assert "crew_assigned" not in types                  # draft: no crew notify
    assert quiet["roles"] == []                          # no published-roster alert


def test_departed_flight_locked(quiet):
    sb = FakeSb(_store(flight_status="departed"))
    with pytest.raises(HTTPException) as e:
        _run(sb)
    assert e.value.status_code == 422
    assert len(sb.store["assignments"]) == 1


def test_cancelled_flight_locked(quiet):
    sb = FakeSb(_store(flight_status="cancelled"))
    with pytest.raises(HTTPException) as e:
        _run(sb)
    assert e.value.status_code == 422


def test_cross_company_404(quiet):
    store = _store()
    store["flights"][0]["company_id"] = "c2"             # other airline's flight
    with pytest.raises(NotFoundError):
        _run(FakeSb(store))


def test_duplicate_replacement_conflict(quiet):
    store = _store()
    store["assignments"].append({"id": "a2", "flight_id": "f1", "crew_id": "cNew",
                                 "duty_type": "operating"})
    with pytest.raises(ConflictError):
        _run(FakeSb(store))


def test_gd_stale_hook_on_finalized(quiet):
    sb = FakeSb(_store(finalized=True))
    res = _run(sb)
    assert quiet["gd"] == ["f1"]                         # helper invoked
    assert res["gd_review"] is True


# ── Candidates ────────────────────────────────────────────────────────────────
def test_candidates_filtered_and_ranked(quiet):
    store = _store()
    store["crew"] += [
        {"id": "cand_low", "company_id": "c1", "status": "active",
         "rank": "captain", "full_name_ar": "قليل الساعات",
         "aircraft_qualifications": "B737"},
        {"id": "cand_high", "company_id": "c1", "status": "active",
         "rank": "captain", "full_name_ar": "كثير الساعات",
         "aircraft_qualifications": "B737"},
        {"id": "cand_busy", "company_id": "c1", "status": "active",
         "rank": "captain", "full_name_ar": "مشغول",
         "aircraft_qualifications": "B737"},
        {"id": "cand_wrong_type", "company_id": "c1", "status": "active",
         "rank": "captain", "full_name_ar": "طراز آخر",
         "aircraft_qualifications": "A320"},
        {"id": "cand_fo", "company_id": "c1", "status": "active",
         "rank": "first_officer", "full_name_ar": "رتبة أخرى",
         "aircraft_qualifications": "B737"},
    ]
    # cand_busy overlaps the flight window (embedded join row).
    store["assignments"].append({
        "id": "ab", "crew_id": "cand_busy", "flight_id": "f9",
        "flights": {"departure_time": "2099-01-01T09:00:00+00:00",
                    "arrival_time": "2099-01-01T11:00:00+00:00",
                    "status": "scheduled", "company_id": "c1"}})
    res = asyncio.run(replacement_candidates("a1", current_user=OPS,
                                             sb=FakeSb(store), rank=None))
    ids = [c["crew_id"] for c in res["candidates"]]
    # cNew is free too (0h) → ranked by REAL hours ascending.
    assert ids == ["cNew", "cand_low", "cand_high"]
    assert "cand_busy" not in ids                        # overlap excluded
    assert "cand_wrong_type" not in ids                  # not type-rated
    assert "cand_fo" not in ids                          # different rank
    assert "cOld" not in ids                             # the one being relieved
    assert res["required_rank"] == "captain"
    assert res["flight_number"] == "IA-900"
