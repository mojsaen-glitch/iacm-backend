"""Safety-governance gate for OM clauses.

Pure decision tests (no DB) + endpoint-level tests (recording fake Supabase)
proving: ops_manager can't disable/downgrade a safety-critical clause,
super_admin can but only with a reason, and every critical change writes an
audit log + a management notification.

Run:  py -m pytest tests/test_om_governance.py -q
"""
import asyncio

import pytest

from app.core.exceptions import ForbiddenError
from app.core.om_governance import (
    evaluate_governance_change, gate_decision, governance_notification,
    is_safety_critical,
)
from app.api.v1.endpoints import om as om_ep
from fastapi import HTTPException


OPS = {"id": "u-ops", "role": "ops_manager", "company_id": "co1"}
SUPER = {"id": "u-sa", "role": "super_admin", "company_id": "co1", "full_name": "SA"}


def _rest_clause(**over):
    base = {"id": "OM-C 9.1", "company_id": "co1", "section": "C",
            "rule_type": "blocking", "affects_compliance": True,
            "bound_check_key": "rest", "is_active": True,
            "title_ar": "الراحة", "title_en": "Rest"}
    base.update(over)
    return base


# ── Pure decision logic ────────────────────────────────────────────────────
def test_critical_classification():
    assert is_safety_critical(True, "rest") is True
    assert is_safety_critical(True, "fdp") is True
    assert is_safety_critical(True, "flight_hours_28day") is True
    assert is_safety_critical(False, "rest") is False        # not governing
    assert is_safety_critical(True, "assignment_conflict") is False  # not critical


def test_disable_is_protected():
    before = _rest_clause()
    after = {**before, "is_active": False}
    protected, kind = evaluate_governance_change(before, after)
    assert protected and kind == "disable"


def test_downgrade_is_protected():
    before = _rest_clause()
    after = {**before, "rule_type": "warning"}
    protected, kind = evaluate_governance_change(before, after)
    assert protected and kind == "downgrade"


def test_rebind_and_unbind_protected():
    before = _rest_clause()
    assert evaluate_governance_change(before, {**before, "bound_check_key": "fdp"})[0]
    assert evaluate_governance_change(before, {**before, "affects_compliance": False})[0]


def test_strengthening_not_protected():
    # Editing the body, or making it blocking, is never gated.
    before = _rest_clause()
    assert evaluate_governance_change(before, {**before, "body_ar": "نص جديد"})[0] is False
    info = _rest_clause(rule_type="informational")
    assert evaluate_governance_change(info, {**info, "rule_type": "blocking"})[0] is False


def test_gate_ops_manager_cannot_disable():
    before = _rest_clause()
    after = {**before, "is_active": False}
    assert gate_decision(OPS, before, after, "any reason")[0] == "forbidden"


def test_gate_ops_manager_cannot_downgrade():
    before = _rest_clause()
    after = {**before, "rule_type": "warning"}
    assert gate_decision(OPS, before, after, "reason")[0] == "forbidden"


def test_gate_super_admin_needs_reason():
    before = _rest_clause()
    after = {**before, "is_active": False}
    assert gate_decision(SUPER, before, after, "")[0] == "reason_required"
    assert gate_decision(SUPER, before, after, "approved by ops")[0] == "ok"


def test_param_change_on_critical_is_protected():
    # Text-only edit on a critical clause is NOT gated.
    before = _rest_clause(parameters={"domestic_min_rest_hours": 10})
    text_only = {**before, "body_ar": "نص محدّث"}
    assert evaluate_governance_change(before, text_only)[0] is False
    # Changing the operational values IS gated (any direction).
    param_change = {**before, "parameters": {"domestic_min_rest_hours": 8}}
    prot, kind = evaluate_governance_change(before, param_change)
    assert prot and kind == "param_change"
    # ops_manager blocked; super_admin needs a reason.
    assert gate_decision(OPS, before, param_change, "x")[0] == "forbidden"
    assert gate_decision(SUPER, before, param_change, "")[0] == "reason_required"
    assert gate_decision(SUPER, before, param_change, "اعتماد امتثال")[0] == "ok"


def test_text_only_change_does_not_change_rule():
    # Editing only the documentation text never trips the gate or alters params.
    before = _rest_clause(parameters={"domestic_min_rest_hours": 10})
    after = {**before, "title_ar": "عنوان جديد", "body_en": "new text"}
    assert evaluate_governance_change(before, after)[0] is False
    assert after["parameters"] == before["parameters"]


def test_notification_text_mentions_clause_and_reason():
    n = governance_notification("OM-C 9.1", "SA", "disable", "صيانة")
    assert "OM-C 9.1" in n["message_ar"]
    assert "صيانة" in n["message_ar"]
    assert "OM-C 9.1" in n["message_en"]


# ── Endpoint-level: recording fake Supabase ────────────────────────────────
class _Resp:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, store, recorder, name):
        self._store, self._rec, self._name = store, recorder, name
        self._eq, self._in = [], []
        self._op, self._payload = "select", None

    def select(self, *_a, **_k): self._op = "select"; return self
    def insert(self, payload): self._op = "insert"; self._payload = payload; return self
    def update(self, payload): self._op = "update"; self._payload = payload; return self
    def delete(self): self._op = "delete"; return self
    def eq(self, c, v): self._eq.append((c, v)); return self
    def in_(self, c, vals): self._in.append((c, set(vals))); return self
    def order(self, *_a, **_k): return self

    def _match(self, rows):
        out = rows
        for c, v in self._eq:
            out = [r for r in out if r.get(c) == v]
        for c, vals in self._in:
            out = [r for r in out if r.get(c) in vals]
        return out

    def execute(self):
        rows = self._store.setdefault(self._name, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            self._rec.setdefault(self._name, []).extend(items)
            rows.extend(items)
            return _Resp(list(items))
        if self._op == "update":
            matched = self._match(rows)
            for r in matched:
                r.update(self._payload)
            self._rec.setdefault(self._name + ":update", []).append(self._payload)
            return _Resp([dict(r) for r in matched])
        if self._op == "delete":
            matched = self._match(rows)
            for r in matched:
                rows.remove(r)
            return _Resp(matched)
        return _Resp([dict(r) for r in self._match(rows)])


class RecSb:
    def __init__(self, store):
        self.store = store
        self.rec = {}
    def table(self, name): return _Q(self.store, self.rec, name)


def _store_with_rest():
    return {
        "om_articles": [_rest_clause()],
        "users": [{"id": "m1", "role": "ops_manager", "company_id": "co1"},
                  {"id": "m2", "role": "super_admin", "company_id": "co1"}],
        "notifications": [],
        "om_rule_audit_logs": [],
    }


def test_endpoint_ops_manager_disable_forbidden():
    sb = RecSb(_store_with_rest())
    with pytest.raises(ForbiddenError):
        asyncio.run(om_ep.update_article(
            "OM-C 9.1", {"is_active": False, "governance_reason": "x"}, OPS, sb))


def test_endpoint_super_admin_disable_needs_reason():
    sb = RecSb(_store_with_rest())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(om_ep.update_article(
            "OM-C 9.1", {"is_active": False}, SUPER, sb))
    assert ei.value.status_code == 422


def test_endpoint_super_admin_disable_audits_and_notifies():
    sb = RecSb(_store_with_rest())
    asyncio.run(om_ep.update_article(
        "OM-C 9.1", {"is_active": False, "governance_reason": "اعتماد مدير العمليات"},
        SUPER, sb))
    audits = sb.rec.get("om_rule_audit_logs", [])
    assert len(audits) == 1
    assert audits[0]["is_safety_critical"] is True
    assert audits[0]["governance_reason"] == "اعتماد مدير العمليات"
    # Management notified (2 management users in the store).
    notifs = sb.rec.get("notifications", [])
    assert len(notifs) == 2
    assert all(n["type"] == "om_governance_change" for n in notifs)
    assert "OM-C 9.1" in notifs[0]["message_ar"]
