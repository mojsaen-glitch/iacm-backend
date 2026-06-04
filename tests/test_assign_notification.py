"""Immediate crew-assignment notification.

Verifies _notify_crew_assigned builds the right in-app notification for the
crew member's login account when they're rostered on a flight — and is a no-op
when the crew has no user account. Uses a tiny fake Supabase client so we test
the real logic without a live DB.

Run:  venv/Scripts/python -m pytest tests/test_assign_notification.py -q
"""
from app.api.v1.endpoints import assignments


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table, store, users):
        self._table = table
        self._store = store
        self._users = users
        self._insert = None

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, rows):
        self._insert = rows
        return self

    def execute(self):
        if self._insert is not None:
            self._store.setdefault(self._table, []).extend(
                self._insert if isinstance(self._insert, list) else [self._insert])
            return _Resp(self._insert)
        if self._table == "users":
            return _Resp(self._users)
        return _Resp([])


class _FakeSb:
    def __init__(self, users):
        self._users = users
        self.store = {}

    def table(self, name):
        return _Query(name, self.store, self._users)


_FLIGHT = {
    "id": "f1",
    "flight_number": "IA-500",
    "departure_time": "2026-05-31T23:42:00Z",
    "origin_code": "BGW",
    "destination_code": "DXB",
}


def test_notify_crew_assigned_creates_notification(monkeypatch):
    monkeypatch.setattr(assignments.push_service, "send_to_users", lambda *a, **k: None)
    sb = _FakeSb(users=[{"id": "user-1"}])

    assignments._notify_crew_assigned(sb, "crew-1", _FLIGHT)

    notifs = sb.store.get("notifications", [])
    assert len(notifs) == 1
    n = notifs[0]
    assert n["user_id"] == "user-1"
    assert n["type"] == "crew_assigned"
    assert n["reference_id"] == "f1"
    assert n["reference_type"] == "flight"
    assert n["is_read"] is False
    assert "IA-500" in n["message_ar"]
    assert "IA-500" in n["message_en"]
    assert "BGW" in n["message_ar"] and "DXB" in n["message_ar"]


def test_notify_crew_assigned_no_user_is_noop(monkeypatch):
    monkeypatch.setattr(assignments.push_service, "send_to_users", lambda *a, **k: None)
    sb = _FakeSb(users=[])  # crew has no login account

    assignments._notify_crew_assigned(sb, "crew-2", _FLIGHT)

    assert sb.store.get("notifications", []) == []


def test_notify_crew_assigned_push_failure_is_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("FCM down")
    monkeypatch.setattr(assignments.push_service, "send_to_users", _boom)
    sb = _FakeSb(users=[{"id": "user-3"}])

    # Push failure must NOT prevent the in-app notification from being saved.
    assignments._notify_crew_assigned(sb, "crew-3", _FLIGHT)
    assert len(sb.store.get("notifications", [])) == 1


def test_notify_crew_assigned_opens_delivery_record(monkeypatch):
    # No device token (web/in-app only) → attempted 0 → status 'sent', no_token.
    monkeypatch.setattr(assignments.push_service, "send_to_users",
                        lambda *a, **k: {"attempted": 0, "succeeded": 0, "failed": 0})
    sb = _FakeSb(users=[{"id": "user-1"}])

    assignments._notify_crew_assigned(sb, "crew-1", _FLIGHT)

    dlv = sb.store.get("notification_delivery", [])
    assert len(dlv) == 1
    d = dlv[0]
    assert d["status"] == "sent"
    assert d["fcm_result"] == "no_token"
    assert d["crew_id"] == "crew-1"
    assert d["flight_id"] == "f1"
    # delivery row is linked to the in-app notification that was created
    assert d["notification_id"] == sb.store["notifications"][0]["id"]


def test_notify_crew_assigned_push_failed_records_failure_and_alerts(monkeypatch):
    # Token present but FCM rejected it → status 'failed' + alert to ops/schedulers.
    monkeypatch.setattr(assignments.push_service, "send_to_users",
                        lambda *a, **k: {"attempted": 1, "succeeded": 0, "failed": 1})
    sb = _FakeSb(users=[{"id": "user-9", "role": "admin"}])
    flight = {**_FLIGHT, "company_id": "co-1"}

    assignments._notify_crew_assigned(sb, "crew-9", flight)

    dlv = sb.store.get("notification_delivery", [])
    assert len(dlv) == 1
    assert dlv[0]["status"] == "failed"
    assert dlv[0]["fcm_result"].startswith("push_failed")
    # an immediate alert notification was raised for the admin
    alerts = [n for n in sb.store.get("notifications", []) if n.get("type") == "delivery_alert"]
    assert len(alerts) == 1
    assert alerts[0]["reference_id"] == "f1"


def test_notify_crew_assigned_stub_mode_is_not_a_failure(monkeypatch):
    # FCM not configured (STUB): tokens exist but nothing is really sent. This
    # must NOT count as a delivery failure and must NOT raise a false alert.
    monkeypatch.setattr(assignments.push_service, "send_to_users",
                        lambda *a, **k: {"attempted": 1, "succeeded": 0,
                                         "failed": 0, "stub": True})
    sb = _FakeSb(users=[{"id": "user-7", "role": "admin"}])
    flight = {**_FLIGHT, "company_id": "co-1"}

    assignments._notify_crew_assigned(sb, "crew-7", flight)

    dlv = sb.store.get("notification_delivery", [])
    assert len(dlv) == 1
    assert dlv[0]["status"] == "sent"
    assert dlv[0]["fcm_result"] == "push_stub"
    # No false "assignment push failed" alert in stub mode.
    alerts = [n for n in sb.store.get("notifications", []) if n.get("type") == "delivery_alert"]
    assert alerts == []
