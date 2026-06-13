"""Reserve/Standby — R1 (FCM + in-app notification on callout).

R1 adds ONLY an alert when a reserve is called out: an in-app `notifications`
row + a best-effort push. NO accept/reject, NO assignment bridge, NO
escalation, NO flight/assignment state change.

Guarantees proven here:
  • notifies the correct (company-scoped) user once, with flight/airport/window,
  • push is attempted but FAIL-SOFT (no token / FCM error never breaks callout),
  • a retry does not re-notify (gated on the ACTIVE→called transition),
  • cancelled / expired reserves never notify,
  • the R0 audit row is still written.

Run:  py -m pytest tests/test_standby_r1.py -q
"""
import asyncio

import pytest

import app.api.v1.endpoints.standby as standby_mod
from app.api.v1.endpoints.standby import callout_standby


# ── filtering + mutating fake (honours .eq()/.in_()) ─────────────────────────
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


ADMIN = {"id": "u1", "name_ar": "مدير العمليات", "role": "admin",
         "company_id": "c1", "is_superuser": False}


def _store(status="ACTIVE", called=False, with_user=True):
    s = {
        "standby_assignments": [
            {"id": "s1", "company_id": "c1", "crew_id": "cr1", "status": status,
             "called_out": called, "assigned_flight_id": None,
             "airport_code": "BGW",
             "start_time": "2099-06-01T08:00:00+00:00",
             "end_time": "2099-06-01T18:00:00+00:00"}],
        "flights": [{"id": "f1", "company_id": "c1", "flight_number": "IA-560"}],
        "notifications": [],
        "users": [],
    }
    if with_user:
        s["users"].append({"id": "u_cr1", "crew_id": "cr1", "company_id": "c1",
                           "is_active": True})
    return s


def _record_push(monkeypatch, result=None, raises=False):
    """Replace push_service.send_to_users with a recorder. Returns the call log."""
    calls = []

    def fake(*a, **k):
        calls.append((a, k))
        if raises:
            raise RuntimeError("FCM down")
        return result or {"attempted": 1, "succeeded": 1, "failed": 0, "stub": False}

    monkeypatch.setattr(standby_mod.push_service, "send_to_users", fake)
    return calls


# ── 1) notification to the correct user, with the details ────────────────────
def test_callout_notifies_correct_user_with_details(monkeypatch):
    _record_push(monkeypatch)
    store = _store()
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    notifs = store["notifications"]
    assert len(notifs) == 1
    n = notifs[0]
    assert n["user_id"] == "u_cr1"
    assert n["type"] == "standby_callout"
    assert n["reference_id"] == "s1" and n["reference_type"] == "standby"
    assert "IA-560" in n["message_ar"]          # flight number
    assert "BGW" in n["message_ar"]             # airport/base
    assert "تم استدعاؤك كاحتياط" in n["message_ar"]


# ── 2) push is attempted for the recipient ───────────────────────────────────
def test_push_attempted_for_recipient(monkeypatch):
    calls = _record_push(monkeypatch)
    store = _store()
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    assert len(calls) == 1
    args, _ = calls[0]
    assert args[1] == ["u_cr1"]                 # send_to_users(sb, [uid], ...)


# ── 3) missing device token must not break callout ───────────────────────────
def test_no_device_token_does_not_break_callout(monkeypatch):
    # push_service returns "nothing sent" — callout must still succeed.
    _record_push(monkeypatch, result={"attempted": 0, "succeeded": 0,
                                       "failed": 0, "stub": False})
    store = _store()
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    assert len(store["notifications"]) == 1                  # in-app still written
    assert store["standby_assignments"][0]["status"] == "ASSIGNED"   # completed


# ── 4) FCM error must not break callout (in-app written before push) ─────────
def test_fcm_failure_does_not_break_callout(monkeypatch):
    _record_push(monkeypatch, raises=True)
    store = _store()
    # No exception should escape.
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    assert len(store["notifications"]) == 1
    assert store["standby_assignments"][0]["status"] == "ASSIGNED"


# ── 5) retry must not duplicate the notification ─────────────────────────────
def test_no_duplicate_notification_on_retry(monkeypatch):
    _record_push(monkeypatch)
    store = _store()
    sb = FakeSb(store)
    asyncio.run(callout_standby("s1", {"flight_id": "f1"}, current_user=ADMIN, sb=sb))
    asyncio.run(callout_standby("s1", {"flight_id": "f1"}, current_user=ADMIN, sb=sb))
    assert len(store["notifications"]) == 1     # second call (called_out=True) skips


# ── 6) cancelled / expired reserves never notify ─────────────────────────────
@pytest.mark.parametrize("status", ["CANCELLED", "EXPIRED"])
def test_terminal_reserve_does_not_notify(monkeypatch, status):
    _record_push(monkeypatch)
    store = _store(status=status)
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    assert store["notifications"] == []


# ── 7) recipient resolution is company-scoped ────────────────────────────────
def test_recipient_is_company_scoped(monkeypatch):
    _record_push(monkeypatch)
    store = _store()
    # A foreign-company user shares the crew_id — must NOT be picked.
    store["users"].append({"id": "u_other", "crew_id": "cr1",
                           "company_id": "c2", "is_active": True})
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    assert len(store["notifications"]) == 1
    assert store["notifications"][0]["user_id"] == "u_cr1"


# ── 8) R0 audit still written; standby state intact ──────────────────────────
def test_callout_still_audits_and_updates(monkeypatch):
    _record_push(monkeypatch)
    store = _store()
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    audits = [a for a in store.get("audit_log", [])
              if a.get("action") == "standby_called_out"]
    assert len(audits) == 1
    assert len(store["notifications"]) == 1
    assert store["standby_assignments"][0]["status"] == "ASSIGNED"


# ── crew without a login account: no recipient, but callout succeeds ─────────
def test_crew_without_user_account_does_not_break(monkeypatch):
    _record_push(monkeypatch)
    store = _store(with_user=False)
    asyncio.run(callout_standby("s1", {"flight_id": "f1"},
                                current_user=ADMIN, sb=FakeSb(store)))
    assert store["notifications"] == []          # nobody to notify
    assert store["standby_assignments"][0]["status"] == "ASSIGNED"   # still done
