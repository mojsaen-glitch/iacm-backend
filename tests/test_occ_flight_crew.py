"""OCC enriched manifest — GET /occ/flights/{id}/crew.

Returns each crew member's name + rank + duty + type-rating for the flight's
current aircraft, plus a crew_review_required flag (drives the drawer badge).

Run:  py -m pytest tests/test_occ_flight_crew.py -q
"""
import asyncio

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError
from app.api.v1.endpoints.occ import occ_flight_crew


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class FakeSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _Q(self.store, name)


OPS       = {"id": "u1", "role": "ops_manager", "company_id": "c1", "is_superuser": False}
CREW_USER = {"id": "u9", "role": "crew",        "company_id": "c1", "is_superuser": False}


def _run(store, user=OPS):
    return asyncio.run(occ_flight_crew("f1", current_user=user, sb=FakeSb(store)))


def test_non_reader_forbidden():
    store = {"flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "A320"}]}
    with pytest.raises(ForbiddenError):
        _run(store, user=CREW_USER)


def test_not_found():
    with pytest.raises(NotFoundError):
        _run({"flights": []})


def test_manifest_has_names_ranks_and_qualification():
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-229",
                     "aircraft_type": "A320", "aircraft_registration": "YI-ASU"}],
        "assignments": [{"crew_id": "cr1", "duty_type": "operating"},
                        {"crew_id": "cr2", "duty_type": "operating"}],
        "crew": [
            {"id": "cr1", "full_name_ar": "زها سمير", "rank": "cabin_crew",
             "aircraft_qualifications": ["A320"]},                 # qualified
            {"id": "cr2", "full_name_ar": "حسن كريم", "rank": "balance",
             "aircraft_qualifications": ["B737"]},                 # NOT for A320
        ],
    }
    res = _run(store)
    # Names are present (no more '--').
    assert {c["name_ar"] for c in res["crew"]} == {"زها سمير", "حسن كريم"}
    by = {c["crew_id"]: c for c in res["crew"]}
    assert by["cr1"]["qualified"] is True
    assert by["cr2"]["qualified"] is False
    assert by["cr1"]["rank"] == "cabin_crew"
    assert res["unqualified_crew"] == 1
    assert res["crew_review_required"] is True


def test_deadhead_unqualified_does_not_trigger_review():
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "aircraft_type": "A320"}],
        "assignments": [{"crew_id": "cr1", "duty_type": "deadhead"}],
        "crew": [{"id": "cr1", "full_name_ar": "x", "aircraft_qualifications": ["B737"]}],
    }
    res = _run(store)
    assert res["crew"][0]["operating"] is False
    assert res["crew_review_required"] is False   # non-operating crew don't gate
