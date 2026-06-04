"""Crew messaging scope — a crew member's contacts = operational managers
(from `users`) + ALL crew on their own flights (from `crew` DIRECTLY, even
without a login account). Crew on other flights are excluded.

Run:  py -m pytest tests/test_message_scoping.py -q
"""
from app.api.v1.endpoints.messages import _crew_contacts, _crew_flight_mate_ids


class _Q:
    """Fake query honouring eq / neq / in_ filters on the rows."""
    def __init__(self, store, name):
        self.rows = list(store.get(name, []))
    def select(self, *a, **k): return self
    def eq(self, c, v):
        self.rows = [r for r in self.rows if r.get(c) == v]; return self
    def neq(self, c, v):
        self.rows = [r for r in self.rows if r.get(c) != v]; return self
    def in_(self, c, vs):
        s = set(vs); self.rows = [r for r in self.rows if r.get(c) in s]; return self
    def order(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": list(self.rows)})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


ME = {"id": "u_me", "company_id": "co1", "role": "crew", "crew_id": "cm1", "is_active": True}


def _store():
    return {
        "users": [
            ME,
            {"id": "u_admin", "company_id": "co1", "role": "admin", "crew_id": None,
             "is_active": True, "name_ar": "مدير", "name_en": "Admin"},
        ],
        # Crew records — note: these crew have NO user accounts, yet must be reachable.
        "crew": [
            {"id": "cm1", "full_name_ar": "أنا", "full_name_en": "Me", "rank": "senior"},
            {"id": "cm2", "full_name_ar": "زميل", "full_name_en": "Mate", "rank": "cabin_crew"},
            {"id": "cm3", "full_name_ar": "غريب", "full_name_en": "Stranger", "rank": "purser"},
        ],
        "assignments": [
            {"crew_id": "cm1", "flight_id": "f1"},   # me
            {"crew_id": "cm2", "flight_id": "f1"},   # flight-mate (same flight) — no account
            {"crew_id": "cm3", "flight_id": "f2"},   # different flight → excluded
        ],
    }


def test_flight_mate_ids_same_flight_only():
    ids = _crew_flight_mate_ids(ME, FakeSb(_store()))
    assert ids == {"cm2"}                     # cm3 (other flight) excluded, self excluded


def test_contacts_are_flight_crew_only():
    contacts = _crew_contacts(ME, FakeSb(_store()))
    by_id = {c["id"]: c for c in contacts}
    # ONLY the flight-mate crew — from the crew table, no login account needed.
    assert "cm2" in by_id and by_id["cm2"]["type"] == "crew"
    assert by_id["cm2"]["name_ar"] == "زميل"
    # Managers are NOT shown to crew anymore.
    assert "u_admin" not in by_id
    # Crew on a different flight is excluded; never self.
    assert "cm3" not in by_id
    assert "cm1" not in by_id


def test_crew_with_no_flights_has_no_contacts():
    store = _store()
    store["assignments"] = []
    contacts = _crew_contacts(ME, FakeSb(store))
    assert contacts == []
