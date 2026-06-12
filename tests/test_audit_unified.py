"""M4 — unified audit trail.

write_audit() is THE way to record sensitive operations: standard row shape,
company stamped from the actor, secrets redacted, reason captured, best-effort.
A drift guard freezes the legacy direct writers so every NEW call site must go
through the helper.

Run:  py -m pytest tests/test_audit_unified.py -q
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from app.core.audit import write_audit


class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self._op, self._payload, self._filters = "select", None, []

    def select(self, *a, **k): return self
    def eq(self, f, v): self._filters.append((f, v)); return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self

    def execute(self):
        if self._op == "update":
            hit = [r for r in self.sb.store.get(self.table, [])
                   if all(r.get(f) == v for f, v in self._filters)]
            for r in hit:
                r.update(self._payload)
            return _R([dict(r) for r in hit])
        if self._op == "insert":
            if self.table in self.sb.fail_insert:
                raise RuntimeError("forced failure")
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            self.sb.store.setdefault(self.table, []).extend(dict(i) for i in items)
            return _R(items)
        return _R(list(self.sb.store.get(self.table, [])))


class FakeSb:
    def __init__(self):
        self.store = {}
        self.fail_insert = set()

    def table(self, name): return _Q(self, name)


USER = {"id": "u1", "name_ar": "مدير العمليات", "email": "x@y.z",
        "company_id": "c1", "role": "ops_manager"}


def test_full_standard_record():
    sb = FakeSb()
    ok = write_audit(sb, USER, "assignment_replaced", "assignment", "a1",
                     before={"crew_id": "old"}, after={"crew_id": "new"})
    assert ok is True
    row = sb.store["audit_log"][0]
    assert row["user_id"] == "u1"
    assert row["user_name"] == "مدير العمليات"
    assert row["action"] == "assignment_replaced"
    assert row["entity_type"] == "assignment" and row["entity_id"] == "a1"
    assert row["company_id"] == "c1"                  # stamped from the actor
    assert json.loads(row["before_data"]) == {"crew_id": "old"}
    assert json.loads(row["after_data"]) == {"crew_id": "new"}
    assert row["created_at"]


def test_company_stamped_from_actor_prevents_mixing():
    sb = FakeSb()
    write_audit(sb, USER, "x", "flight", "f1", after={"k": 1})
    write_audit(sb, {**USER, "id": "u2", "company_id": "c2"}, "x", "flight", "f2")
    rows = sb.store["audit_log"]
    assert rows[0]["company_id"] == "c1" and rows[1]["company_id"] == "c2"


def test_reason_lands_in_after_data_and_override_fields():
    sb = FakeSb()
    write_audit(sb, USER, "override_assignment", "assignment", "a1",
                after={"crew_id": "c"}, reason="ضرورة تشغيلية",
                is_override=True, override_reason="ضرورة تشغيلية")
    row = sb.store["audit_log"][0]
    assert json.loads(row["after_data"])["reason"] == "ضرورة تشغيلية"
    assert row["is_override"] is True
    assert row["override_reason"] == "ضرورة تشغيلية"


def test_secrets_are_redacted():
    sb = FakeSb()
    write_audit(sb, USER, "account_update", "user", "u9",
                before={"hashed_password": "abc", "nested": {"api_token": "t"}},
                after={"totp_secret": "s", "refresh_token": "r",
                       "name_ar": "علي", "items": [{"password": "p"}]})
    row = sb.store["audit_log"][0]
    b, a = json.loads(row["before_data"]), json.loads(row["after_data"])
    assert b["hashed_password"] == "***"
    assert b["nested"]["api_token"] == "***"
    assert a["totp_secret"] == "***" and a["refresh_token"] == "***"
    assert a["items"][0]["password"] == "***"
    assert a["name_ar"] == "علي"                      # normal data untouched


def test_failure_never_raises():
    sb = FakeSb()
    sb.fail_insert.add("audit_log")
    assert write_audit(sb, USER, "x", "flight", "f1") is False


# ── New coverage: the three previously-unaudited operations ──────────────────
def test_block_and_unblock_crew_are_audited():
    from app.api.v1.endpoints.crew import block_crew, unblock_crew
    sb = FakeSb()
    sb.store["crew"] = [{"id": "cr1", "company_id": "c1", "status": "active"}]
    asyncio.run(block_crew("cr1", {"reason": "طبي"}, current_user=USER, sb=sb))
    asyncio.run(unblock_crew("cr1", current_user=USER, sb=sb))
    actions = [r["action"] for r in sb.store["audit_log"]]
    assert actions == ["crew_blocked", "crew_unblocked"]
    blocked = sb.store["audit_log"][0]
    assert json.loads(blocked["after_data"])["reason"] == "طبي"
    assert blocked["company_id"] == "c1"


def test_create_flight_is_audited():
    from app.api.v1.endpoints.flights import create_flight
    sb = FakeSb()
    sb.store["flights"] = []
    asyncio.run(create_flight({
        "flight_number": "IA-1", "origin_code": "BGW", "destination_code": "EBL",
        "departure_time": "2099-01-01T10:00:00+00:00",
        "arrival_time": "2099-01-01T12:00:00+00:00",
        "aircraft_registration": "YI-ASA", "company_id": "c1",
    }, current_user=USER, sb=sb))
    audits = [r for r in sb.store.get("audit_log", [])
              if r["action"] == "flight_created"]
    assert len(audits) == 1
    body = json.loads(audits[0]["after_data"])
    assert body["flight_number"] == "IA-1"
    assert body["aircraft_registration"] == "YI-ASA"


# ── Drift guard: new code must use write_audit, not raw inserts ──────────────
# Frozen counts of the LEGACY direct writers (they all already conform to the
# standard shape). Adding a NEW raw `table("audit_log")` call anywhere fails
# this test — route it through app.core.audit.write_audit instead.
_FROZEN_DIRECT_WRITERS = {
    "admin_control.py": 1, "admin_metrics.py": 1, "assignments.py": 5,
    "auth.py": 1, "crew.py": 3, "developer.py": 1, "developer_actions.py": 1,
    "developer_vercel.py": 1, "documents.py": 1, "flights.py": 12, "occ.py": 2,
}


def test_no_new_direct_audit_writers():
    base = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "endpoints"
    pat = re.compile(r'table\(\s*"audit_log"\s*\)')
    for f in sorted(base.glob("*.py")):
        n = len(pat.findall(f.read_text(encoding="utf-8")))
        allowed = _FROZEN_DIRECT_WRITERS.get(f.name, 0)
        assert n <= allowed, (
            f"{f.name}: {n} direct audit_log writers (frozen at {allowed}) — "
            f"new audit writes must use app.core.audit.write_audit")
