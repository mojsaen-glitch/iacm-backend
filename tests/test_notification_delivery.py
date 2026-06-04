"""Notification-delivery lazy timeout transitions.

Verifies _apply_delivery_timeouts moves stale delivery rows to the right state
without a cron: sent→delivery_not_confirmed (>3min, no delivered ACK) and
delivered→unread_after_deadline (>10min, no read ACK), while fresh/terminal
rows are left untouched. Uses a no-op fake Supabase client (the persist step is
best-effort and irrelevant to the returned status).

Run:  venv/Scripts/python -m pytest tests/test_notification_delivery.py -q
"""
from datetime import datetime, timezone, timedelta
from app.api.v1.endpoints import notifications as N


class _Q:
    def update(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): return type("R", (), {"data": []})()


class _Sb:
    def table(self, name): return _Q()


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_sent_past_3min_becomes_not_confirmed():
    rows = [{"id": "d1", "status": "sent", "sent_at": _ago(5), "delivered_at": None}]
    out = N._apply_delivery_timeouts(_Sb(), rows)
    assert out[0]["status"] == "delivery_not_confirmed"


def test_sent_within_3min_stays_sent():
    rows = [{"id": "d2", "status": "sent", "sent_at": _ago(1), "delivered_at": None}]
    out = N._apply_delivery_timeouts(_Sb(), rows)
    assert out[0]["status"] == "sent"


def test_delivered_past_10min_becomes_unread():
    rows = [{"id": "d3", "status": "delivered", "delivered_at": _ago(15), "read_at": None}]
    out = N._apply_delivery_timeouts(_Sb(), rows)
    assert out[0]["status"] == "unread_after_deadline"


def test_delivered_within_10min_stays_delivered():
    rows = [{"id": "d4", "status": "delivered", "delivered_at": _ago(3), "read_at": None}]
    out = N._apply_delivery_timeouts(_Sb(), rows)
    assert out[0]["status"] == "delivered"


def test_read_row_is_untouched():
    rows = [{"id": "d5", "status": "read", "delivered_at": _ago(60), "read_at": _ago(50)}]
    out = N._apply_delivery_timeouts(_Sb(), rows)
    assert out[0]["status"] == "read"


def test_failed_row_is_not_auto_transitioned():
    # 'failed' (push rejected) already fired an alert — keep it visible.
    rows = [{"id": "d6", "status": "failed", "sent_at": _ago(30), "delivered_at": None}]
    out = N._apply_delivery_timeouts(_Sb(), rows)
    assert out[0]["status"] == "failed"
