"""Flight notification READ-receipts + mark-as-read ownership.

GET /flights/{id}/notification-receipts — who was notified / who read / when.
POST /notifications/{id}/read — owner-only, idempotent, preserves read_at.

Run:  py -m pytest tests/test_notification_receipts.py -q
"""
import asyncio

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError
from app.api.v1.endpoints.flights import flight_notification_receipts
from app.api.v1.endpoints.notifications import mark_read


# ── Recording fake (filters ignored) — for the receipts endpoint ─────────────
class _Q:
    def __init__(self, store, name): self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, p): self.store.setdefault(self.name + "_inserts", []).append(p); return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


# ── Filtering fake — for mark_read (ownership depends on eq filters) ─────────
class _FQ:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.rows = list(store.get(name, []))
        self._patch = None
    def select(self, *a, **k): return self
    def update(self, patch): self._patch = patch; return self
    def eq(self, col, val):
        self.rows = [r for r in self.rows if r.get(col) == val]
        return self
    def is_(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def execute(self):
        if self._patch is not None:
            for r in self.rows:
                r.update(self._patch)
            self.store.setdefault(self.name + "_updates", []).append(
                {"patch": self._patch, "count": len(self.rows)})
        return type("R", (), {"data": list(self.rows)})()


class FilterSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _FQ(self.store, name)


SCHEDULER = {"id": "u1", "role": "scheduler", "company_id": "c1", "is_superuser": False}
CREW_USER = {"id": "u9", "role": "crew", "company_id": "c1", "crew_id": "cr9",
             "is_superuser": False}


def _receipt_store():
    return {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-229",
                     "origin_code": "BGW", "destination_code": "EBL",
                     "departure_time": "2026-06-20T10:00:00+00:00",
                     "publish_status": "published"}],
        "notifications": [
            {"id": "n1", "user_id": "u_cr1", "type": "flight_published",
             "title_ar": "رحلة", "related_flight_id": "f1",
             "is_read": True, "read_at": "2026-06-12T09:00:00+00:00",
             "created_at": "2026-06-12T08:00:00+00:00"},
            {"id": "n2", "user_id": "u_cr2", "type": "crew_assigned",
             "title_ar": "تكليف", "related_flight_id": "f1",
             "is_read": False, "read_at": None,
             "created_at": "2026-06-12T08:01:00+00:00"},
            {"id": "n3", "user_id": "u_cr1", "type": "flight_unpublished",
             "title_ar": "سحب", "related_flight_id": "f1",
             "is_read": False, "read_at": None,
             "created_at": "2026-06-12T08:02:00+00:00"},
        ],
        "users": [{"id": "u_cr1", "crew_id": "cr1", "role": "crew"},
                  {"id": "u_cr2", "crew_id": "cr2", "role": "crew"}],
        "crew": [{"id": "cr1", "full_name_ar": "زها سمير", "rank": "cabin_crew",
                  "primary_phone": "0770"},
                 {"id": "cr2", "full_name_ar": "حسن كريم", "rank": "pilot_captain"}],
    }


def _run(store, user=SCHEDULER):
    return asyncio.run(flight_notification_receipts(
        "f1", current_user=user, sb=FakeSb(store)))


# ── Access control ────────────────────────────────────────────────────────────
def test_scheduler_can_view_receipts():
    res = _run(_receipt_store())
    assert res["flight_number"] == "IA-229"


def test_crew_forbidden():
    with pytest.raises(ForbiddenError):
        _run(_receipt_store(), user=CREW_USER)


def test_cross_company_404():
    store = _receipt_store()
    store["flights"] = []          # flight not visible in caller's company
    with pytest.raises(NotFoundError):
        _run(store)


# ── Content ───────────────────────────────────────────────────────────────────
def test_summary_counts_and_read_at():
    res = _run(_receipt_store())
    s = res["summary"]
    assert s["total_sent"] == 3
    assert s["total_read"] == 1
    assert s["total_unread"] == 2
    assert s["read_percentage"] == 33
    assert s["last_read_at"] == "2026-06-12T09:00:00+00:00"
    by_id = {i["notification_id"]: i for i in res["items"]}
    assert by_id["n1"]["status"] == "read" and by_id["n1"]["read_at"]
    assert by_id["n2"]["status"] == "unread" and by_id["n2"]["read_at"] is None
    assert by_id["n1"]["crew_name"] == "زها سمير"
    assert by_id["n2"]["crew_rank"] == "pilot_captain"


def test_unpublish_and_assignment_types_included():
    types = {i["notification_type"] for i in _run(_receipt_store())["items"]}
    assert {"flight_unpublished", "crew_assigned", "flight_published"} <= types


def test_view_is_audited():
    store = _receipt_store()
    _run(store)
    assert any(a["action"] == "view_flight_notification_receipts"
               for a in store.get("audit_log_inserts", []))


# ── mark-as-read ──────────────────────────────────────────────────────────────
def test_owner_marks_read_sets_read_at():
    store = {"notifications": [
        {"id": "n1", "user_id": "u9", "is_read": False, "read_at": None}],
        "notification_delivery": []}
    asyncio.run(mark_read("n1", current_user=CREW_USER, sb=FilterSb(store)))
    assert store["notifications"][0]["is_read"] is True
    assert store["notifications"][0]["read_at"]


def test_cannot_mark_others_notification():
    store = {"notifications": [
        {"id": "n1", "user_id": "SOMEONE_ELSE", "is_read": False, "read_at": None}]}
    with pytest.raises(NotFoundError):
        asyncio.run(mark_read("n1", current_user=CREW_USER, sb=FilterSb(store)))
    assert store["notifications"][0]["is_read"] is False    # untouched


def test_mark_read_idempotent_preserves_read_at():
    original = "2026-06-12T09:00:00+00:00"
    store = {"notifications": [
        {"id": "n1", "user_id": "u9", "is_read": True, "read_at": original}]}
    res = asyncio.run(mark_read("n1", current_user=CREW_USER, sb=FilterSb(store)))
    assert res["message"]
    assert store["notifications"][0]["read_at"] == original   # NOT overwritten
