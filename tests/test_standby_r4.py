"""Reserve/Standby — R4 (compliance gate at standby creation).

create_standby now runs the EXISTING ComplianceEngine over the standby window
(no parallel logic). HARD blocks (crew blocked/inactive, expired docs/training,
time conflict, missing type rating, engine error) prevent creating an ACTIVE
reserve and return a clear reason. The FTL family (rest/FDP/hours) and
WARNING/CRITICAL issues are advisory only — the reserve is created and the
warnings are surfaced (no override is opened at creation).

The engine is stubbed here to drive create_standby's REACTION to each outcome;
the engine's own rules are covered by the compliance suite.

Run:  py -m pytest tests/test_standby_r4.py -q
"""
import asyncio

import pytest
from fastapi import HTTPException

import app.api.v1.endpoints.standby as standby_mod
from app.api.v1.endpoints.standby import create_standby
from app.core.exceptions import NotFoundError


# ── filtering + mutating fake ────────────────────────────────────────────────
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters, self._op, self._payload = [], "select", None

    def select(self, *a, **k): self._op = "select"; return self
    def eq(self, f, v): self._filters.append((f, v)); return self
    def in_(self, f, vals): self._filters.append((f, list(vals))); return self
    def order(self, *a, **k): return self
    def insert(self, p): self._op, self._payload = "insert", p; return self
    def update(self, p): self._op, self._payload = "update", p; return self
    def delete(self): self._op = "delete"; return self

    def _match(self, r):
        for f, v in self._filters:
            if isinstance(v, list):
                if r.get(f) not in v:
                    return False
            elif r.get(f) != v:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            rows.extend(dict(i) for i in items)
            return _R([dict(i) for i in items])
        hits = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in hits:
                r.update(self._payload)
            return _R([dict(r) for r in hits])
        if self._op == "delete":
            self.store[self.name] = [r for r in rows if not self._match(r)]
            return _R([dict(r) for r in hits])
        return _R([dict(r) for r in hits])


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


ADMIN = {"id": "u1", "name_ar": "مدير", "role": "admin",
         "company_id": "c1", "is_superuser": False}

PAYLOAD = {"crew_id": "cr1", "standby_type": "HOME_STANDBY",
           "start_time": "2099-06-01T08:00:00+00:00",
           "end_time": "2099-06-01T18:00:00+00:00", "response_minutes": 90}


def _store(crew_company="c1"):
    return {"crew": [{"id": "cr1", "company_id": crew_company}],
            "standby_assignments": []}


def _engine(monkeypatch, issues=None, status="OK"):
    """Stub ComplianceEngine.check_crew with a fixed result."""
    class _E:
        def __init__(self, sb): pass
        def check_crew(self, **k):
            return {"status": status, "issues": issues or []}
    monkeypatch.setattr(standby_mod, "ComplianceEngine", _E)


def _issue(rule, blocking, severity, msg):
    return {"rule": rule, "is_blocking": blocking, "severity": severity,
            "message_ar": msg, "message_en": msg}


# ── valid crew creates an ACTIVE reserve ─────────────────────────────────────
def test_valid_crew_creates_active(monkeypatch):
    _engine(monkeypatch, issues=[])
    store = _store()
    res = asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    assert res["status"] == "ACTIVE" and res["warnings"] == []
    assert len(store["standby_assignments"]) == 1


# ── HARD blocks prevent creation (no ACTIVE row), with a clear reason ────────
@pytest.mark.parametrize("rule,msg", [
    ("crew_blocked", "الموظف محظور"),
    ("crew_status_inactive", "الموظف غير نشط"),
    ("conflict_overlap", "تعارض زمني مع رحلة مكلّف بها"),
    ("documents_expired_passport", "منتهٍ: جواز السفر"),
    ("training_expired_recurrent", "منتهٍ: التدريب الدوري"),
    ("aircraft_qualification_missing", "لا يملك تأهيل الطراز"),
])
def test_hard_block_prevents_creation(monkeypatch, rule, msg):
    _engine(monkeypatch, issues=[_issue(rule, True, "BLOCKING", msg)])
    store = _store()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    assert ei.value.status_code == 409
    assert msg in ei.value.detail
    assert store["standby_assignments"] == []        # nothing created


def test_engine_unknown_is_a_hard_block(monkeypatch):
    _engine(monkeypatch, status="UNKNOWN")
    store = _store()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    assert ei.value.status_code == 409
    assert store["standby_assignments"] == []


# ── other-company crew fails before the engine even runs ─────────────────────
def test_other_company_crew_fails(monkeypatch):
    _engine(monkeypatch, issues=[])
    store = _store(crew_company="c2")        # crew not in ADMIN's company
    with pytest.raises(NotFoundError):
        asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    assert store["standby_assignments"] == []


# ── FTL family → WARNING only, reserve is still created ──────────────────────
def test_ftl_block_is_warning_not_rejection(monkeypatch):
    _engine(monkeypatch, issues=[_issue("fdp_exceeded", True, "BLOCKING",
                                        "تجاوز حد FDP اليومي")])
    store = _store()
    res = asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    assert res["status"] == "ACTIVE"                 # created despite FTL
    assert "تجاوز حد FDP اليومي" in res["warnings"]
    assert len(store["standby_assignments"]) == 1


def test_rest_and_hours_are_warnings(monkeypatch):
    _engine(monkeypatch, issues=[
        _issue("rest_insufficient", True, "BLOCKING", "راحة غير كافية"),
        _issue("hours_monthly_high", True, "BLOCKING", "ساعات شهرية مرتفعة"),
    ])
    store = _store()
    res = asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    assert res["status"] == "ACTIVE"
    assert set(res["warnings"]) == {"راحة غير كافية", "ساعات شهرية مرتفعة"}


# ── non-blocking WARNING/CRITICAL surfaced; pure INFO ignored ────────────────
def test_warning_and_critical_surface_info_ignored(monkeypatch):
    _engine(monkeypatch, issues=[
        _issue("documents_expiring_medical", False, "WARNING", "قرب الانتهاء: الطبية"),
        _issue("crm_due_soon", False, "CRITICAL", "تدريب CRM قريب"),
        _issue("some_info", False, "INFO", "معلومة فقط"),
    ])
    store = _store()
    res = asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    assert res["status"] == "ACTIVE"
    assert "قرب الانتهاء: الطبية" in res["warnings"]
    assert "تدريب CRM قريب" in res["warnings"]
    assert "معلومة فقط" not in res["warnings"]        # INFO is not surfaced


# ── audit still records creation (with warnings) ─────────────────────────────
def test_create_audit_includes_warnings(monkeypatch):
    _engine(monkeypatch, issues=[_issue("fdp_exceeded", True, "BLOCKING", "تجاوز FDP")])
    store = _store()
    asyncio.run(create_standby(dict(PAYLOAD), current_user=ADMIN, sb=FakeSb(store)))
    import json
    audits = [a for a in store.get("audit_log", []) if a["action"] == "standby_created"]
    assert len(audits) == 1
    after = json.loads(audits[0]["after_data"])
    assert "تجاوز FDP" in after["warnings"]
