"""Publish mandatory-complement gate + delete_crew roster protection.

Run:  py -m pytest tests/test_publish_gate_and_crew_delete.py -q
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

import app.api.v1.endpoints.flights as fl_mod
import app.api.v1.endpoints.crew as crew_mod
from app.api.v1.endpoints.flights import publish_flight
from app.api.v1.endpoints.crew import delete_crew
from app.services import push_service


class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, f, v):  self._filters.append(("eq", f, v));  return self
    def neq(self, f, v): self._filters.append(("neq", f, v)); return self
    def in_(self, f, v): self._filters.append(("in", f, set(v))); return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self
    def delete(self):    self._op = "delete"; return self

    def _match(self, row):
        for op, f, v in self._filters:
            if op == "eq" and row.get(f) != v: return False
            if op == "neq" and row.get(f) == v: return False
            if op == "in" and row.get(f) not in v: return False
        return True

    def execute(self):
        rows = self.sb.store.get(self.table, [])
        if self._op == "insert":
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
            gone = [r for r in rows if self._match(r)]
            self.sb.store[self.table] = [r for r in rows if not self._match(r)]
            self.sb.ops.append(("delete", self.table, gone))
            return _R(gone)
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store):
        self.store = {k: [dict(r) for r in v] for k, v in store.items()}
        self.ops = []

    def table(self, name): return _Q(self, name)

    def inserted(self, table):
        return [i for op, t, items in self.ops
                if op == "insert" and t == table for i in items]


ADMIN = {"id": "u-adm", "role": "admin", "company_id": "c1",
         "name_ar": "الإدارة", "is_superuser": False}


def _crew(cid, rank):
    return {"id": cid, "company_id": "c1", "rank": rank, "status": "active",
            "full_name_ar": f"عضو {cid}"}


def _flight(**over):
    f = {"id": "f1", "company_id": "c1", "flight_number": "IA-560",
         "origin_code": "BGW", "destination_code": "BGW",
         "aircraft_type": "A320", "aircraft_registration": "YI-ASA",
         "publish_status": "draft", "status": "scheduled",
         "roster_finalized_status": "", "gd_status": ""}
    f.update(over)
    return f


def _asg(aid, crew_id, duty="operating"):
    return {"id": aid, "flight_id": "f1", "crew_id": crew_id, "duty_type": duty}


@pytest.fixture
def quiet(monkeypatch):
    calls = {"gd": [], "roles": []}
    monkeypatch.setattr(fl_mod, "mark_gd_stale_if_finalized",
                        lambda sb, cid, fid, actor=None: calls["gd"].append(fid))
    monkeypatch.setattr(fl_mod, "_insert_role_notifications",
                        lambda sb, cid, roles, ntype, *a, **k:
                        calls["roles"].append((ntype, a[-1] if a else None)) or 1)
    monkeypatch.setattr(push_service, "send_to_users", lambda *a, **k: 0)
    return calls


# ── C: publish gate ───────────────────────────────────────────────────────────
def test_publish_rejects_missing_fo(quiet):
    """1 captain + 3 CC but no F/O → pilots 1/2 → 422 with the shortfall."""
    sb = FakeSb({
        "flights": [_flight()],
        "crew": [_crew("cap", "captain"), _crew("cc1", "cabin_crew"),
                 _crew("cc2", "cabin_crew"), _crew("cc3", "cabin_crew")],
        "assignments": [_asg("a1", "cap"), _asg("a2", "cc1"),
                        _asg("a3", "cc2"), _asg("a4", "cc3")],
        "users": [], "notifications": [], "audit_log": [],
    })
    with pytest.raises(HTTPException) as e:
        asyncio.run(publish_flight("f1", current_user=ADMIN, sb=sb))
    assert e.value.status_code == 422
    assert "الطيارين" in str(e.value.detail)            # pilots 1/2 named
    f = sb.store["flights"][0]
    assert f["publish_status"] == "draft"               # nothing published


def test_publish_rejects_missing_cabin(quiet):
    """Full cockpit but only 2 operating CC (a third rides deadhead) → 422."""
    sb = FakeSb({
        "flights": [_flight()],
        "crew": [_crew("cap", "captain"), _crew("fo", "first_officer"),
                 _crew("cc1", "cabin_crew"), _crew("cc2", "cabin_crew"),
                 _crew("cc3", "cabin_crew")],
        "assignments": [_asg("a1", "cap"), _asg("a2", "fo"),
                        _asg("a3", "cc1"), _asg("a4", "cc2"),
                        _asg("a5", "cc3", duty="deadhead")],   # rider ≠ operating
        "users": [], "notifications": [], "audit_log": [],
    })
    with pytest.raises(HTTPException) as e:
        asyncio.run(publish_flight("f1", current_user=ADMIN, sb=sb))
    assert e.value.status_code == 422
    assert "المقصورة" in str(e.value.detail)


def test_publish_allows_when_mandatory_met_even_without_advisory(quiet):
    """capt + F/O + 3 CC and NO AME / loadmaster / security → publishes."""
    sb = FakeSb({
        "flights": [_flight()],
        "crew": [_crew("cap", "captain"), _crew("fo", "first_officer"),
                 _crew("cc1", "cabin_crew"), _crew("cc2", "cabin_crew"),
                 _crew("cc3", "cabin_crew")],
        "assignments": [_asg("a1", "cap"), _asg("a2", "fo"),
                        _asg("a3", "cc1"), _asg("a4", "cc2"), _asg("a5", "cc3")],
        "users": [], "notifications": [], "audit_log": [],
    })
    asyncio.run(publish_flight("f1", current_user=ADMIN, sb=sb))
    assert sb.store["flights"][0]["publish_status"] == "published"


# ── B: delete_crew protection ─────────────────────────────────────────────────
def _delete_store(publish="published", finalized=True):
    return {
        "crew": [_crew("cX", "captain")],
        "flights": [_flight(publish_status=publish,
                            roster_finalized_status="finalized" if finalized else "",
                            gd_status="ready" if finalized else "")],
        "assignments": [{"id": "a1", "flight_id": "f1", "crew_id": "cX",
                         "duty_type": "operating", "assigned_role": "captain"}],
        "audit_log": [], "notifications": [], "users": [],
    }


def test_delete_crew_marks_gd_stale(quiet):
    sb = FakeSb(_delete_store(finalized=True))
    asyncio.run(delete_crew("cX", current_user=ADMIN, sb=sb))
    assert sb.store["crew"] == []                        # member gone
    assert sb.store["assignments"] == []                 # duties gone
    assert quiet["gd"] == ["f1"]                         # stale hook fired


def test_delete_crew_writes_audit(quiet):
    sb = FakeSb(_delete_store())
    asyncio.run(delete_crew("cX", current_user=ADMIN, sb=sb))
    audits = sb.inserted("audit_log")
    assert len(audits) == 1
    a = audits[0]
    assert a["action"] == "assignment_removed_by_crew_delete"
    body = json.loads(a["after_data"])
    assert body["crew_id"] == "cX"
    assert body["crew_name"] == "عضو cX"
    assert body["flight_id"] == "f1"
    assert body["flight_number"] == "IA-560"
    assert body["assignment_id"] == "a1"
    assert body["reason"] == "crew_deleted"


def test_delete_crew_alerts_ops_for_live_flight(quiet):
    sb = FakeSb(_delete_store(publish="published", finalized=False))
    asyncio.run(delete_crew("cX", current_user=ADMIN, sb=sb))
    assert quiet["roles"] and quiet["roles"][0][0] == "roster_changed"
    assert quiet["gd"] == []                             # not finalized → no stale


def test_delete_crew_quiet_for_draft_unassigned(quiet):
    """Draft flight → no ops alert; crew with no duties → no audit noise."""
    sb = FakeSb(_delete_store(publish="draft", finalized=False))
    asyncio.run(delete_crew("cX", current_user=ADMIN, sb=sb))
    assert quiet["roles"] == []                          # draft: no alert
    assert len(sb.inserted("audit_log")) == 1            # audit still written

    sb2 = FakeSb({"crew": [_crew("cY", "captain")], "flights": [],
                  "assignments": [], "audit_log": [], "notifications": []})
    asyncio.run(delete_crew("cY", current_user=ADMIN, sb=sb2))
    assert sb2.inserted("audit_log") == []               # nothing to audit
