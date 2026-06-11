"""Draft-roster visibility — crew accounts must NOT see unpublished flights.

Workflow: schedulers (all tiers) collaborate on a DRAFT roster; crew see (and
are notified about) a duty only when the flight is PUBLISHED. These tests pin
the publish filter on both crew-facing reads:
  • GET /crew/{id}/flights        (crew portal "upcoming duties")
  • GET /assignments              (crew role → own rows, published only)

Run:  py -m pytest tests/test_publish_visibility.py -q
"""
import asyncio

from app.api.v1.endpoints.crew import get_crew_flights
from app.api.v1.endpoints.assignments import get_assignments


# Fake Supabase that RECORDS every eq() filter so tests can assert exactly
# which constraints each role's query carries.
class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, col, val):
        self.store.setdefault("_eqs", []).append((self.name, col, val))
        return self
    def neq(self, *a, **k): return self
    def gte(self, col, val):
        self.store.setdefault("_gtes", []).append((self.name, col, val))
        return self
    def lte(self, col, val):
        self.store.setdefault("_ltes", []).append((self.name, col, val))
        return self
    def in_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def range(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


CREW_USER = {"id": "u1", "role": "crew", "company_id": "c1", "crew_id": "cr1",
             "is_superuser": False}
ADMIN     = {"id": "u2", "role": "admin", "company_id": "c1", "is_superuser": False}


def _store():
    return {
        "crew": [{"id": "cr1"}],
        "assignments": [{"flight_id": "f1", "crew_id": "cr1"}],
        "flights": [{"id": "f1", "publish_status": "draft",
                     "departure_time": "2099-01-01T10:00:00+00:00"}],
    }


def _eqs(store):
    return store.get("_eqs", [])


# ── Crew portal: /crew/{id}/flights ───────────────────────────────────────────
def test_crew_portal_flights_filtered_to_published():
    store = _store()
    asyncio.run(get_crew_flights("cr1", current_user=CREW_USER, sb=FakeSb(store)))
    assert ("flights", "publish_status", "published") in _eqs(store)


def test_admin_view_includes_drafts():
    store = _store()
    asyncio.run(get_crew_flights("cr1", current_user=ADMIN, sb=FakeSb(store)))
    assert ("flights", "publish_status", "published") not in _eqs(store)


# ── Assignments list: GET /assignments ────────────────────────────────────────
def test_crew_assignments_filtered_to_published():
    store = _store()
    asyncio.run(get_assignments(current_user=CREW_USER, sb=FakeSb(store),
                                flight_id=None, crew_id=None,
                                from_date=None, to_date=None,
                                page=1, page_size=100))
    eqs = _eqs(store)
    assert ("assignments", "flights.publish_status", "published") in eqs
    # And still force-narrowed to their own crew_id.
    assert ("assignments", "crew_id", "cr1") in eqs


def test_scheduler_assignments_see_drafts():
    # Schedulers collaborate on the draft — NO publish filter for them.
    store = _store()
    asyncio.run(get_assignments(current_user=ADMIN, sb=FakeSb(store),
                                flight_id=None, crew_id=None,
                                from_date=None, to_date=None,
                                page=1, page_size=100))
    assert ("assignments", "flights.publish_status", "published") not in _eqs(store)


# ── H3: windowed fetch — from/to pushed down to the DB via the flights join ──
def test_assignments_date_window_pushed_to_db():
    store = _store()
    asyncio.run(get_assignments(current_user=ADMIN, sb=FakeSb(store),
                                flight_id=None, crew_id=None,
                                from_date="2026-06-01", to_date="2026-06-20",
                                page=1, page_size=100))
    assert ("assignments", "flights.departure_time", "2026-06-01") in store.get("_gtes", [])
    assert ("assignments", "flights.departure_time", "2026-06-20T23:59:59") in store.get("_ltes", [])


def test_assignments_no_window_when_unbounded():
    store = _store()
    asyncio.run(get_assignments(current_user=ADMIN, sb=FakeSb(store),
                                flight_id=None, crew_id=None,
                                from_date=None, to_date=None,
                                page=1, page_size=100))
    assert not [g for g in store.get("_gtes", []) if g[1] == "flights.departure_time"]
