"""
FCM push notification service.

Operates in two modes:
  1. LIVE — when firebase-admin can initialize from a service-account JSON
     pointed to by FIREBASE_SERVICE_ACCOUNT_JSON (env var, file path or
     raw JSON string), pushes go out via FCM.
  2. STUB — when credentials are missing, calls are logged and silently
     succeed. The in-app notification flow keeps working so the product
     never breaks because Firebase isn't set up yet.

All errors are swallowed and logged — push delivery is best-effort and
must NEVER block the primary notification write.
"""
import os
import json
import logging
from typing import Iterable, Optional

log = logging.getLogger(__name__)

_initialized = False
_messaging = None
_app = None


def _init_once() -> bool:
    """Lazy init — returns True only when LIVE mode is available."""
    global _initialized, _messaging, _app
    if _initialized:
        return _messaging is not None

    _initialized = True
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        log.info("push_service: FIREBASE_SERVICE_ACCOUNT_JSON not set — STUB mode")
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials, messaging

        if os.path.isfile(raw):
            cred = credentials.Certificate(raw)
        else:
            cred = credentials.Certificate(json.loads(raw))

        _app = firebase_admin.initialize_app(cred, name="iacm-push")
        _messaging = messaging
        log.info("push_service: initialized in LIVE mode")
        return True
    except Exception as e:
        log.warning("push_service: failed to init firebase-admin (%s) — STUB mode", e)
        _messaging = None
        return False


def send_to_users(
    sb,
    user_ids: Iterable[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    badge: Optional[int] = None,
) -> dict:
    """
    Push to every device token belonging to `user_ids`.

    Returns: {"attempted": int, "succeeded": int, "failed": int, "stub": bool}
    Never raises. Cleans up invalid tokens automatically.
    """
    user_ids = [u for u in user_ids if u]
    if not user_ids:
        return {"attempted": 0, "succeeded": 0, "failed": 0, "stub": False}

    live = _init_once()

    # Fetch active device tokens for these users
    try:
        res = sb.table("device_tokens").select("token,user_id,platform") \
            .in_("user_id", user_ids).execute()
        tokens = [row for row in (res.data or []) if row.get("token")]
    except Exception as e:
        log.warning("push_service: failed to list device tokens — %s", e)
        return {"attempted": 0, "succeeded": 0, "failed": 0, "stub": not live}

    if not tokens:
        return {"attempted": 0, "succeeded": 0, "failed": 0, "stub": not live}

    if not live:
        log.info("push_service[STUB]: would push '%s' to %d devices",
                 title, len(tokens))
        return {"attempted": len(tokens), "succeeded": 0,
                "failed": 0, "stub": True}

    # LIVE — fan out via FCM (max 500 tokens per multicast)
    succeeded = 0
    failed = 0
    invalid_tokens: list[str] = []

    extras = {k: str(v) for k, v in (data or {}).items()}

    for chunk_start in range(0, len(tokens), 500):
        chunk = tokens[chunk_start:chunk_start + 500]
        msg = _messaging.MulticastMessage(
            notification=_messaging.Notification(title=title, body=body),
            data=extras,
            tokens=[t["token"] for t in chunk],
            android=_messaging.AndroidConfig(
                priority="high",
                notification=_messaging.AndroidNotification(
                    sound="default",
                    channel_id="iacm_high_importance",
                ),
            ),
            apns=_messaging.APNSConfig(
                payload=_messaging.APNSPayload(
                    aps=_messaging.Aps(
                        sound="default",
                        badge=badge,
                        content_available=True,
                    ),
                ),
            ),
        )
        try:
            resp = _messaging.send_each_for_multicast(msg)
            succeeded += resp.success_count
            failed += resp.failure_count
            for i, r in enumerate(resp.responses):
                if not r.success and r.exception is not None:
                    err = str(r.exception)
                    # Common FCM errors for invalid/expired tokens
                    if ("registration-token-not-registered" in err
                            or "invalid-argument" in err
                            or "not a valid FCM registration token" in err):
                        invalid_tokens.append(chunk[i]["token"])
        except Exception as e:
            log.warning("push_service: multicast failed — %s", e)
            failed += len(chunk)

    # Clean up dead tokens — best-effort
    if invalid_tokens:
        try:
            sb.table("device_tokens").delete().in_("token", invalid_tokens).execute()
            log.info("push_service: pruned %d dead tokens", len(invalid_tokens))
        except Exception as e:
            log.warning("push_service: failed to prune dead tokens — %s", e)

    return {
        "attempted": len(tokens),
        "succeeded": succeeded,
        "failed":    failed,
        "stub":      False,
    }
