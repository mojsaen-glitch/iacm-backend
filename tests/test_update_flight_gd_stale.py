"""Plain flight edits AFTER roster approval must invalidate the GD —
update_flight now runs the same stale hook as remove/replace/OCC.

Run:  py -m pytest tests/test_update_flight_gd_stale.py -q
"""
import asyncio
import json

import app.api.v1.endpoints.flights as fl_mod
from app.api.v1.endpoints.flights import update_flight, gd_clearance


class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def eq(self, f, v):  self._filters.append(("eq", f, v));  return self
    def in_(self, f, v): self._filters.append(("in", f, set(v))); return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self

    def _match(self, row):
        for op, f, v in self._filters:
            if op == "eq" and row.get(f) != v: return False
            if op == "in" and row.get(f) not in v: return False
        return True

    def execute(self):
        rows = self.sb.store.get(self.table, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            self.sb.store[self.table] = rows
            self.sb.ops.append(("insert", self.table, items))
            return _R(items)
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            self.sb.ops.append(("update", self.table, self._payload))
            return _R([dict(r) for r in hit])
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store):
        self.store = {k: [dict(r) for r in v] for k, v in store.items()}
        self.ops = []

    def table(self, name): return _Q(self, name)

    def audits(self, action):
        return [i for op, t, items in self.ops
                if op == "insert" and t == "audit_log"
                for i in items if i.get("action") == action]


OPS = {"id": "u1", "role": "ops_manager", "company_id": "c1",
       "name_ar": "العمليات", "is_superuser": False}


def _store(finalized=True):
    """Finalized + published flight with a FULL cleared complement, so the
    clearance test isolates the stale reason."""
    members = [("cap", "captain"), ("fo", "first_officer"),
               ("cc1", "cabin_crew"), ("cc2", "cabin_crew"), ("cc3", "cabin_crew")]
    return {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-77",
                     "origin_code": "BGW", "destination_code": "EBL",
                     "aircraft_type": "A320", "aircraft_registration": "YI-ASA",
                     "departure_time": "2099-01-01T10:00:00+00:00",
                     "arrival_time": "2099-01-01T12:00:00+00:00",
                     "publish_status": "published",
                     "roster_finalized_status": "finalized" if finalized else "",
                     "gd_status": "ready" if finalized else "", "gd_version": 1}],
        "crew": [{"id": c, "company_id": "c1", "rank": r, "status": "active",
                  "full_name_ar": f"عضو {c}", "passport_number": f"P-{c}"}
                 for c, r in members],
        "assignments": [{"id": f"a{i}", "flight_id": "f1", "crew_id": c,
                         "duty_type": "operating", "acknowledged": True,
                         "declined": False, "admin_confirmed": False}
                        for i, (c, _r) in enumerate(members)],
        "audit_log": [],
    }


def _patch(sb, body):
    return asyncio.run(update_flight("f1", body, current_user=OPS, sb=sb))


def _quiet(monkeypatch):
    calls = []
    monkeypatch.setattr(fl_mod, "_insert_role_notifications",
                        lambda sb, cid, roles, ntype, *a, **k:
                        calls.append(ntype) or 1)
    return calls


def _flight(sb):
    return sb.store["flights"][0]


# ── Impactful edits AFTER approval ⇒ GD stale ────────────────────────────────
def test_departure_time_change_stales_gd(monkeypatch):
    notes = _quiet(monkeypatch)
    sb = FakeSb(_store())
    _patch(sb, {"departure_time": "2099-01-01T14:00:00+00:00",
                "arrival_time": "2099-01-01T16:00:00+00:00"})
    assert _flight(sb)["gd_status"] == "stale"
    assert notes == ["gd_stale"]                     # ops alerted (hook policy)
    audits = sb.audits("flight_updated_after_finalize")
    assert len(audits) == 1
    after = json.loads(audits[0]["after_data"])
    assert "departure_time" in after["changed_fields"]
    assert after["gd_invalidated"] is True


def test_reg_change_stales_gd(monkeypatch):
    _quiet(monkeypatch)
    sb = FakeSb(_store())
    _patch(sb, {"aircraft_registration": "YI-AQW"})
    assert _flight(sb)["gd_status"] == "stale"
    assert sb.audits("flight_updated_after_finalize")


def test_aircraft_type_change_stales_gd(monkeypatch):
    _quiet(monkeypatch)
    sb = FakeSb(_store())
    _patch(sb, {"aircraft_type": "B737"})
    assert _flight(sb)["gd_status"] == "stale"


# ── Non-finalized / non-impactful edits stay quiet ───────────────────────────
def test_not_finalized_edit_never_stales(monkeypatch):
    notes = _quiet(monkeypatch)
    sb = FakeSb(_store(finalized=False))
    _patch(sb, {"aircraft_type": "B737",
                "departure_time": "2099-01-01T14:00:00+00:00",
                "arrival_time": "2099-01-01T15:00:00+00:00"})
    assert _flight(sb)["gd_status"] == ""            # untouched
    assert notes == []
    assert not sb.audits("flight_updated_after_finalize")


def test_non_impact_field_keeps_gd_ready(monkeypatch):
    notes = _quiet(monkeypatch)
    sb = FakeSb(_store())
    _patch(sb, {"gate": "B7", "notes": "ملاحظة تشغيلية"})
    assert _flight(sb)["gd_status"] == "ready"
    assert notes == [] and not sb.audits("flight_updated_after_finalize")


def test_same_instant_resend_is_not_a_change(monkeypatch):
    """Edit forms resend the whole payload — the same moment in 'Z' shape must
    not invalidate the GD."""
    notes = _quiet(monkeypatch)
    sb = FakeSb(_store())
    _patch(sb, {"departure_time": "2099-01-01T10:00:00Z",
                "arrival_time": "2099-01-01T12:00:00Z",
                "aircraft_registration": "yi-asa"})   # normalizes to YI-ASA
    assert _flight(sb)["gd_status"] == "ready"
    assert notes == []


# ── The GD gate refuses the flight once stale ────────────────────────────────
def test_clearance_blocks_after_stale_edit(monkeypatch):
    _quiet(monkeypatch)
    sb = FakeSb(_store())
    _patch(sb, {"departure_time": "2099-01-01T14:00:00+00:00",
                "arrival_time": "2099-01-01T16:00:00+00:00"})
    res = asyncio.run(gd_clearance("f1", current_user=OPS, sb=sb))
    assert res["allowed"] is False
    assert any("إعادة الاعتماد/إعادة توليد" in r for r in res["reasons"])
