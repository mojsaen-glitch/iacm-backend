"""Batch 3 — OCC reason codes read per-company settings (delay + aircraft
change only). No row ⇒ today's constants; custom list accepted; removed
default rejected; per-company isolation; fallback on broken value.

Run:  py -m pytest tests/test_occ_reason_codes_settings.py -q
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

from app.api.v1.endpoints.occ import (
    occ_delay_flight, occ_change_aircraft,
    _allowed_codes, _DELAY_REASON_CODES, _AIRCRAFT_CHANGE_REASONS,
)


# ── Recording fake (ignores filters) — for endpoint accept/reject ────────────
class _RecQ:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def insert(self, p):
        self.store.setdefault(self.name + "_inserts", []).append(p); return self
    def update(self, p):
        self.store.setdefault(self.name + "_updates", []).append(p); return self
    def execute(self):
        return type("R", (), {"data": list(self.store.get(self.name, []))})()


class RecSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _RecQ(self.store, name)


# ── Filtering fake (honours company_id + key) — for isolation ────────────────
class _FiltQ:
    def __init__(self, store, name):
        self.store, self.name, self._f = store, name, []
    def select(self, *a, **k): return self
    def eq(self, f, v): self._f.append((f, v)); return self
    def in_(self, *a, **k): return self
    def execute(self):
        rows = [r for r in self.store.get(self.name, [])
                if all(r.get(f) == v for f, v in self._f)]
        return type("R", (), {"data": rows})()


class FiltSb:
    def __init__(self, store): self.store = store
    def table(self, name): return _FiltQ(self.store, name)


OPS = {"id": "u1", "role": "ops_manager", "company_id": "c1", "is_superuser": False}


def _iso(h):
    return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat()


def _settings_row(cid, key, value):
    return {"company_id": cid, "key": key, "value": json.dumps(value)}


# ── _allowed_codes helper ────────────────────────────────────────────────────
def test_allowed_codes_defaults_when_no_row():
    sb = RecSb({})
    assert _allowed_codes(sb, "c1", "ops.delay.reason_codes",
                          _DELAY_REASON_CODES) == set(_DELAY_REASON_CODES)


def test_allowed_codes_custom_list():
    sb = RecSb({"settings": [
        _settings_row("c1", "ops.delay.reason_codes",
                      ["weather", "vip", "other"])]})
    assert _allowed_codes(sb, "c1", "ops.delay.reason_codes",
                          _DELAY_REASON_CODES) == {"weather", "vip", "other"}


def test_allowed_codes_empty_or_broken_falls_back():
    sb = RecSb({"settings": [
        {"company_id": "c1", "key": "ops.delay.reason_codes", "value": "{bad"}]})
    assert _allowed_codes(sb, "c1", "ops.delay.reason_codes",
                          _DELAY_REASON_CODES) == set(_DELAY_REASON_CODES)


def test_allowed_codes_company_isolation():
    sb = FiltSb({"settings": [
        _settings_row("cA", "ops.delay.reason_codes", ["weather", "vip"])]})
    assert _allowed_codes(sb, "cA", "ops.delay.reason_codes",
                          _DELAY_REASON_CODES) == {"weather", "vip"}
    assert _allowed_codes(sb, "cB", "ops.delay.reason_codes",
                          _DELAY_REASON_CODES) == set(_DELAY_REASON_CODES)


# ── Delay endpoint honours the custom list ───────────────────────────────────
def _delay_store(codes=None):
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-1",
                     "origin_code": "BGW", "destination_code": "CAI",
                     "departure_time": _iso(1), "arrival_time": _iso(3),
                     "status": "scheduled", "aircraft_registration": "YI-ASU"}],
        "assignments": [], "users": [],
    }
    if codes is not None:
        store["settings"] = [_settings_row("c1", "ops.delay.reason_codes", codes)]
    return store


def test_delay_default_codes_still_work_without_settings():
    store = _delay_store()
    res = asyncio.run(occ_delay_flight("f1", {"new_etd": _iso(3),
                      "reason_code": "weather"}, current_user=OPS,
                      sb=RecSb(store)))
    assert res["ok"] is True


def test_delay_accepts_custom_code_and_rejects_removed_default():
    # company allows a NEW code 'vip' and drops 'commercial'
    store = _delay_store(["weather", "vip", "other"])
    res = asyncio.run(occ_delay_flight("f1", {"new_etd": _iso(3),
                      "reason_code": "vip"}, current_user=OPS, sb=RecSb(store)))
    assert res["ok"] is True

    store2 = _delay_store(["weather", "vip", "other"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(occ_delay_flight("f1", {"new_etd": _iso(3),
                    "reason_code": "commercial"}, current_user=OPS,
                    sb=RecSb(store2)))
    assert e.value.status_code == 422
    assert "flights_updates" not in store2          # nothing persisted


# ── Change-aircraft endpoint honours the custom list ─────────────────────────
def _change_store(codes=None):
    store = {
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-1",
                     "origin_code": "BGW", "destination_code": "CAI",
                     "departure_time": _iso(2), "arrival_time": _iso(4),
                     "status": "scheduled", "aircraft_type": "A320",
                     "aircraft_registration": "YI-OLD"}],
        "aircraft": [], "assignments": [], "crew": [], "users": [],
    }
    if codes is not None:
        store["settings"] = [
            _settings_row("c1", "ops.aircraft_change.reason_codes", codes)]
    return store


def test_change_aircraft_rejects_non_allowed_after_customization():
    # valid REG + type so we reach the reason-code gate; 'capacity' was dropped
    store = _change_store(["maintenance", "vip_swap"])
    with pytest.raises(HTTPException) as e:
        asyncio.run(occ_change_aircraft(
            "f1", {"aircraft_registration": "YI-NEW", "aircraft_type": "B737",
                   "reason_code": "capacity"}, current_user=OPS, sb=RecSb(store)))
    assert e.value.status_code == 422
    assert "سبب التغيير" in str(e.value.detail)        # the reason gate, not REG
    assert "flights_updates" not in store


def test_change_aircraft_default_codes_work_without_settings():
    store = _change_store()
    res = asyncio.run(occ_change_aircraft(
        "f1", {"aircraft_registration": "YI-NEW", "aircraft_type": "B737",
               "reason_code": "maintenance"}, current_user=OPS, sb=RecSb(store)))
    assert res.get("ok") is True or res.get("flight_id") == "f1"


def test_change_aircraft_accepts_custom_code():
    store = _change_store(["maintenance", "vip_swap"])
    res = asyncio.run(occ_change_aircraft(
        "f1", {"aircraft_registration": "YI-NEW", "aircraft_type": "B737",
               "reason_code": "vip_swap"}, current_user=OPS, sb=RecSb(store)))
    assert res.get("ok") is True or res.get("flight_id") == "f1"
