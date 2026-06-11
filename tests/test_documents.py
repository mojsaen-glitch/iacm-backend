"""Crew Documents (text-only) — RBAC, validation, verification, audit,
compliance impact, reminders.

Run:  py -m pytest tests/test_documents.py -q
"""
import asyncio
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.compliance_engine import ComplianceEngine, Severity
import app.api.v1.endpoints.documents as docs_mod
from app.api.v1.endpoints.documents import (
    create_document, update_document, verify_document, delete_document,
    get_crew_documents, check_expiring_reminders, cron_expiry_reminders,
    _clean_and_validate,
)
from app.api.deps import get_current_user
from app.core.exceptions import UnauthorizedError


# ── Fake Supabase ─────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, store, name): self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, p): self.store.setdefault(self.name + "_inserts", []).append(p); return self
    def update(self, p): self.store.setdefault(self.name + "_updates", []).append(p); return self
    def delete(self): self.store.setdefault(self.name + "_deletes", []).append(True); return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


EDITOR = {"id": "u1", "role": "compliance_officer", "company_id": "c1", "is_superuser": False}
ADMIN  = {"id": "u2", "role": "admin",              "company_id": "c1", "is_superuser": False}
CREW   = {"id": "u9", "role": "crew",               "company_id": "c1", "is_superuser": False}


def _valid_doc(**over):
    d = {"crew_id": "cr1", "document_type": "passport", "document_number": "P123",
         "issued_by": "Civil Aviation Authority",
         "issue_date": "2026-01-01", "expiry_date": "2030-01-01"}
    d.update(over)
    return d


def _audits(store, action):
    return [a for a in store.get("audit_log_inserts", []) if a.get("action") == action]


# ── Read access control ───────────────────────────────────────────────────────
CREW_CR1   = {"id": "u_cr1", "role": "crew", "company_id": "c1", "crew_id": "cr1", "is_superuser": False}
VIEWER     = {"id": "uv", "role": "viewer", "company_id": "c1", "is_superuser": False}


def _store_with_docs():
    return {"crew": [{"id": "cr1"}],
            "documents": [{"id": "d1", "crew_id": "cr1", "document_type": "passport",
                           "issue_date": "2026-01-01", "expiry_date": "2030-01-01",
                           "is_verified": True, "issued_by": "X"}]}


def test_admin_reads_same_company_docs():
    res = asyncio.run(get_crew_documents("cr1", current_user=ADMIN, sb=FakeSb(_store_with_docs())))
    assert len(res) == 1 and res[0]["expiry_status"] == "valid"


def test_compliance_officer_reads_docs():
    res = asyncio.run(get_crew_documents("cr1", current_user=EDITOR, sb=FakeSb(_store_with_docs())))
    assert len(res) == 1


def test_crew_reads_own_docs():
    res = asyncio.run(get_crew_documents("cr1", current_user=CREW_CR1, sb=FakeSb(_store_with_docs())))
    assert len(res) == 1


def test_crew_cannot_read_other_crew_docs():
    with pytest.raises(ForbiddenError):
        asyncio.run(get_crew_documents("cr2", current_user=CREW_CR1, sb=FakeSb(_store_with_docs())))


def test_viewer_cannot_read_docs():
    with pytest.raises(ForbiddenError):
        asyncio.run(get_crew_documents("cr1", current_user=VIEWER, sb=FakeSb(_store_with_docs())))


def test_cross_company_read_blocked():
    # Editor in c1 requesting a crew not present in c1 → 404 (NotFoundError).
    with pytest.raises(NotFoundError):
        asyncio.run(get_crew_documents("cr_other", current_user=ADMIN, sb=FakeSb({"crew": []})))


def test_unauthorized_request_401():
    creds = type("C", (), {"credentials": "not-a-valid-jwt"})()
    with pytest.raises(UnauthorizedError):
        asyncio.run(get_current_user(credentials=creds, sb=FakeSb({})))


# ── RBAC ──────────────────────────────────────────────────────────────────────
def test_create_unauthorized_403():
    with pytest.raises(ForbiddenError):
        asyncio.run(create_document(_valid_doc(), current_user=CREW, sb=FakeSb({"crew": [{"id": "cr1"}]})))


def test_create_authorized_success():
    store = {"crew": [{"id": "cr1"}], "documents": []}
    res = asyncio.run(create_document(_valid_doc(), current_user=EDITOR, sb=FakeSb(store)))
    assert res["is_verified"] is False
    assert store["documents_inserts"]
    assert _audits(store, "create_document")


def test_update_unauthorized_403():
    with pytest.raises(ForbiddenError):
        asyncio.run(update_document("d1", {"notes": "x"}, current_user=CREW, sb=FakeSb({})))


def test_verify_unauthorized_403():
    with pytest.raises(ForbiddenError):
        asyncio.run(verify_document("d1", current_user=CREW, sb=FakeSb({}), data=None))


def test_delete_unauthorized_403():
    with pytest.raises(ForbiddenError):
        asyncio.run(delete_document("d1", current_user=CREW, sb=FakeSb({})))


# ── Validation (422) ──────────────────────────────────────────────────────────
def test_invalid_document_type_422():
    with pytest.raises(HTTPException) as ei:
        _clean_and_validate(_valid_doc(document_type="banana"))
    assert ei.value.status_code == 422


def test_invalid_issue_date_422():
    with pytest.raises(HTTPException) as ei:
        _clean_and_validate(_valid_doc(issue_date="not-a-date"))
    assert ei.value.status_code == 422


def test_invalid_expiry_date_422():
    with pytest.raises(HTTPException) as ei:
        _clean_and_validate(_valid_doc(expiry_date="32/13/2020"))
    assert ei.value.status_code == 422


def test_expiry_before_issue_422():
    with pytest.raises(HTTPException) as ei:
        _clean_and_validate(_valid_doc(issue_date="2030-01-01", expiry_date="2026-01-01"))
    assert ei.value.status_code == 422


def test_missing_number_for_important_type_422():
    d = _valid_doc(); d.pop("document_number")
    with pytest.raises(HTTPException) as ei:
        _clean_and_validate(d)
    assert ei.value.status_code == 422


def test_other_requires_notes_422():
    with pytest.raises(HTTPException) as ei:
        _clean_and_validate({"document_type": "other", "issued_by": "x",
                             "issue_date": "2026-01-01", "expiry_date": "2030-01-01"})
    assert ei.value.status_code == 422


def test_valid_passport_passes():
    out = _clean_and_validate(_valid_doc())
    assert out["document_type"] == "passport"
    assert "file_path" not in out          # legacy column never written by new logic


def test_issuing_authority_alias_maps_to_issued_by():
    out = _clean_and_validate({"document_type": "visa", "document_number": "V1",
                               "issuing_authority": "MOI", "issue_date": "2026-01-01",
                               "expiry_date": "2030-01-01"})
    assert out["issued_by"] == "MOI"


# ── Verify ────────────────────────────────────────────────────────────────────
def test_verify_sets_flags_and_audits():
    store = {"documents": [{"id": "d1", "crew_id": "cr1", "is_verified": False}]}
    res = asyncio.run(verify_document("d1", current_user=EDITOR, sb=FakeSb(store), data=None))
    upd = store["documents_updates"][0]
    assert upd["is_verified"] is True
    assert upd["verified_by"] == "u1"
    assert upd["verified_at"]
    assert _audits(store, "verify_document")


# ── Audit on update / delete ──────────────────────────────────────────────────
def test_update_audits():
    store = {"documents": [{"id": "d1", "crew_id": "cr1", "document_type": "passport",
                            "document_number": "P1", "issued_by": "x",
                            "issue_date": "2026-01-01", "expiry_date": "2030-01-01"}]}
    asyncio.run(update_document("d1", {"expiry_date": "2031-01-01"}, current_user=EDITOR, sb=FakeSb(store)))
    assert _audits(store, "update_document")
    assert store["documents_updates"][0]["expiry_date"] == "2031-01-01"


def test_delete_audits():
    store = {"documents": [{"id": "d1", "crew_id": "cr1"}]}
    asyncio.run(delete_document("d1", current_user=EDITOR, sb=FakeSb(store)))
    assert store.get("documents_deletes")
    assert _audits(store, "delete_document")


# ── Compliance impact ─────────────────────────────────────────────────────────
def _eng():
    return ComplianceEngine(FakeSb({}))


def test_expired_document_blocking():
    issues = _eng()._check_documents("cr1", docs=[
        {"document_type": "medical_certificate", "expiry_date": "2020-01-01", "is_verified": True}])
    assert any(i.severity == Severity.BLOCKING and i.rule.startswith("doc_expired") for i in issues)


def test_unverified_document_review_not_blocking():
    issues = _eng()._check_documents("cr1", docs=[
        {"document_type": "passport", "expiry_date": "2030-01-01", "is_verified": False}])
    assert any(i.rule.startswith("doc_unverified") and i.severity == Severity.WARNING for i in issues)
    assert not any(i.severity == Severity.BLOCKING for i in issues)


def test_malformed_date_not_silently_ignored():
    issues = _eng()._check_documents("cr1", docs=[
        {"document_type": "passport", "expiry_date": "garbage", "is_verified": True}])
    assert any(i.rule.startswith("doc_invalid_date") for i in issues)  # surfaced, not skipped


# ── Reminders ─────────────────────────────────────────────────────────────────
def test_reminders_require_permission():
    with pytest.raises(ForbiddenError):
        asyncio.run(check_expiring_reminders(current_user=CREW, sb=FakeSb({})))


def test_reminders_detect_expiring(monkeypatch):
    monkeypatch.setattr(docs_mod.push_service, "send_to_users", lambda *a, **k: None)
    soon = (date.today() + timedelta(days=10)).isoformat()
    store = {
        "documents": [{"id": "d1", "crew_id": "cr1", "document_type": "passport",
                       "expiry_date": soon, "last_reminder_sent": None,
                       "crew": {"id": "cr1", "company_id": "c1", "full_name_ar": "زيد"}}],
        "users": [{"id": "u_cr1", "crew_id": "cr1", "role": "crew"}],
    }
    res = asyncio.run(check_expiring_reminders(current_user=ADMIN, sb=FakeSb(store)))
    assert res["sent"] == 1
    assert store.get("notifications_inserts")


# ── Cron ──────────────────────────────────────────────────────────────────────
def test_cron_rejects_bad_secret(monkeypatch):
    monkeypatch.setattr(docs_mod.settings, "CRON_SECRET", "s3cret")
    with pytest.raises(ForbiddenError):
        asyncio.run(cron_expiry_reminders(sb=FakeSb({}), authorization="Bearer wrong"))


def test_cron_runs_with_valid_secret(monkeypatch):
    monkeypatch.setattr(docs_mod.settings, "CRON_SECRET", "s3cret")
    monkeypatch.setattr(docs_mod.push_service, "send_to_users", lambda *a, **k: None)
    store = {"companies": [{"id": "c1", "is_active": True}], "documents": [], "users": []}
    res = asyncio.run(cron_expiry_reminders(sb=FakeSb(store), authorization="Bearer s3cret"))
    assert res["companies"] == 1
