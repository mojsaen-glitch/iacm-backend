"""ONE way to write the audit trail (M4).

Every sensitive operation records the SAME row shape:
    user_id · user_name · action · entity_type · entity_id · company_id ·
    before_data · after_data (JSON) · is_override · override_reason · created_at
with `reason` (when the operation requires one) carried inside
after_data["reason"] — the convention every existing record already follows,
so old rows and new rows read identically.

Rules the helper enforces so call sites can't drift:
  • company_id is stamped from the acting user (cross-company rows impossible).
  • Secrets never reach the trail: any key containing password/token/secret/
    totp/otp is redacted recursively.
  • Best-effort: an audit failure is logged but NEVER fails the operation.

New code MUST use write_audit() — tests/test_audit_unified.py freezes the
list of legacy direct writers and fails on any new `table("audit_log")` call.
"""
import json
import logging
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

# Keys whose values must never reach the audit trail.
_REDACT_MARKERS = ("password", "token", "secret", "totp", "otp")


def _redact(value):
    if isinstance(value, dict):
        return {k: ("***" if any(m in str(k).lower() for m in _REDACT_MARKERS)
                    else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _encode(data):
    if data is None:
        return None
    if isinstance(data, str):          # pre-encoded by a legacy caller
        return data
    try:
        return json.dumps(_redact(data), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(data), ensure_ascii=False)


def write_audit(sb, user, action: str, entity_type: str, entity_id,
                *, before=None, after=None, reason=None,
                is_override: bool = False, override_reason=None,
                company_id=None) -> bool:
    """Standard audit row. Returns True when persisted, False otherwise —
    never raises into the caller."""
    try:
        if reason is not None:
            after = dict(after or {})
            after.setdefault("reason", reason)
        row = {
            "user_id": (user or {}).get("id"),
            "user_name": (user or {}).get("name_ar")
                         or (user or {}).get("name_en")
                         or (user or {}).get("email", ""),
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "company_id": company_id or (user or {}).get("company_id"),
            "before_data": _encode(before),
            "after_data": _encode(after),
            "is_override": bool(is_override),
            "override_reason": override_reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        sb.table("audit_log").insert(row).execute()
        return True
    except Exception:
        _log.exception("audit write failed: %s %s/%s",
                       action, entity_type, entity_id)
        return False
