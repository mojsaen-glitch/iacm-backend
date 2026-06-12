"""GD clearance gate — the scheduling cycle must be COMPLETE before the
official GenDec can be issued/downloaded.

Run:  py -m pytest tests/test_gd_clearance.py -q
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.flights import gd_clearance, log_gendec_download
from app.core.exceptions import NotFoundError


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
        return _R([dict(r) for r in rows if self._match(r)])


class FakeSb:
    def __init__(self, store):
        self.store = {k: [dict(r) for r in v] for k, v in store.items()}
        self.ops = []

    def table(self, name): return _Q(self, name)


OPS = {"id": "u1", "role": "flight_operations", "company_id": "c1",
       "name_ar": "العمليات", "is_superuser": False}


def _store(publish="published", finalized=True, gd_status="ready",
           acceptance="accepted", crew_over=None, drop_fo=False):
    """A fully-cleared flight by default; flags poke one hole at a time."""
    ack = {"accepted": {"acknowledged": True},
           "pending":  {"acknowledged": False},
           "declined": {"declined": True},
           "admin":    {"acknowledged": False, "admin_confirmed": True}}[acceptance]
    members = [("cap", "captain"), ("fo", "first_officer"),
               ("cc1", "cabin_crew"), ("cc2", "cabin_crew"), ("cc3", "cabin_crew")]
    if drop_fo:
        members = [m for m in members if m[0] != "fo"]
    crew = []
    for cid, rank in members:
        row = {"id": cid, "company_id": "c1", "rank": rank, "status": "active",
               "full_name_ar": f"عضو {cid}", "passport_number": f"P-{cid}"}
        row.update((crew_over or {}).get(cid, {}))
        crew.append(row)
    asgs = []
    for i, (cid, _r) in enumerate(members):
        a = {"id": f"a{i}", "flight_id": "f1", "crew_id": cid,
             "duty_type": "operating", "acknowledged": True,
             "declined": False, "admin_confirmed": False}
        if cid == "cc3":           # the acceptance flag is poked on ONE member
            a.update({"acknowledged": False, "declined": False,
                      "admin_confirmed": False, **ack})
        asgs.append(a)
    return {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-560",
                     "origin_code": "BGW", "destination_code": "BGW",
                     "aircraft_type": "A320", "aircraft_registration": "YI-ASA",
                     "publish_status": publish,
                     "roster_finalized_status": "finalized" if finalized else "",
                     "gd_status": gd_status, "gd_version": 1}],
        "crew": crew, "assignments": asgs, "audit_log": [],
    }


def _clearance(store):
    return asyncio.run(gd_clearance("f1", current_user=OPS, sb=FakeSb(store)))


# ── The six required scenarios ────────────────────────────────────────────────
def test_unpublished_blocked_even_with_full_crew():
    res = _clearance(_store(publish="draft"))
    assert res["allowed"] is False
    assert any("قبل نشر الرحلة" in r for r in res["reasons"])


def test_published_but_missing_fo_blocked():
    res = _clearance(_store(drop_fo=True))
    assert res["allowed"] is False
    assert any("الطيارين" in r for r in res["reasons"])     # pilots 1/2


def test_pending_acceptance_blocked_with_name():
    res = _clearance(_store(acceptance="pending"))
    assert res["allowed"] is False
    assert any("عضو cc3" in r and "لم يوافق بعد" in r for r in res["reasons"])


def test_declined_blocked_with_name():
    res = _clearance(_store(acceptance="declined"))
    assert res["allowed"] is False
    assert any("عضو cc3" in r and "رفض" in r for r in res["reasons"])


def test_stale_gd_requires_regeneration():
    res = _clearance(_store(gd_status="stale"))
    assert res["allowed"] is False
    assert any("إعادة الاعتماد/إعادة توليد" in r for r in res["reasons"])


def test_fully_cleared_flight_allowed():
    res = _clearance(_store())
    assert res["allowed"] is True
    assert res["reasons"] == []
    assert res["gd_status"] == "ready"


# ── Extra gate layers ─────────────────────────────────────────────────────────
def test_not_finalized_blocked():
    res = _clearance(_store(finalized=False, gd_status=""))
    assert res["allowed"] is False
    assert any("اعتماد الجدول النهائي" in r for r in res["reasons"])


def test_missing_passport_blocked_with_name():
    res = _clearance(_store(crew_over={"cc2": {"passport_number": ""}}))
    assert res["allowed"] is False
    assert any("جواز السفر غير مسجّل" in r and "عضو cc2" in r
               for r in res["reasons"])


def test_admin_confirmed_member_passes():
    res = _clearance(_store(acceptance="admin"))
    assert res["allowed"] is True


# ── Server-side enforcement on the download trail ─────────────────────────────
def test_log_download_refused_when_blocked():
    sb = FakeSb(_store(publish="draft"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(log_gendec_download("f1", current_user=OPS, sb=sb,
                                        data={"format": "pdf"}))
    assert e.value.status_code == 422
    assert "قبل نشر الرحلة" in str(e.value.detail)
    assert not [o for o in sb.ops if o[0] == "insert"]   # no audit for refusal


def test_log_download_allowed_and_audited_when_cleared():
    sb = FakeSb(_store())
    res = asyncio.run(log_gendec_download("f1", current_user=OPS, sb=sb,
                                          data={"format": "doc"}))
    assert res["ok"] is True
    audits = [i for op, t, items in sb.ops if op == "insert" and t == "audit_log"
              for i in items]
    assert len(audits) == 1 and audits[0]["action"] == "gd_downloaded"


def test_log_download_cross_company_404():
    store = _store()
    store["flights"][0]["company_id"] = "c2"
    with pytest.raises(NotFoundError):
        asyncio.run(log_gendec_download("f1", current_user=OPS,
                                        sb=FakeSb(store), data=None))
