"""Safety-governance gate for OM clauses.

Pure decision logic (no DB / no FastAPI) so it can be unit-tested directly.
The HTTP layer (endpoints/om.py) calls these and turns the result into the
role check, the required-reason check, the audit entry, and the management
notification.

Rule of the gate: a clause bound to a SAFETY-CRITICAL check may not be
*weakened* — disabled, downgraded from blocking→warning/informational,
re-bound to a different check, or un-bound from compliance — by anyone below
Super Admin, and every such change must carry a written governance_reason.
Strengthening (e.g. adding a blocking critical rule) is never restricted.
"""
from __future__ import annotations

# Check families that protect flight safety. Mirrors ComplianceEngine binding
# keys; flight-hours windows are all included (fatigue limits).
SAFETY_CRITICAL_CHECKS = {
    "rest",
    "fdp",
    "training",
    "documents",
    "aircraft_qualification",
    "flight_hours_28day",
    "flight_hours_yearly",
    "flight_hours_monthly",
}

_BLOCKING_TYPES = {"blocking", "approval_required"}
_WEAK_TYPES = {"warning", "informational"}


def is_safety_critical(affects_compliance, bound_check_key) -> bool:
    return bool(affects_compliance) and bound_check_key in SAFETY_CRITICAL_CHECKS


def _b(row: dict, key: str, default=None):
    return row.get(key, default)


def evaluate_governance_change(before: dict, after: dict) -> tuple[bool, str | None]:
    """Does moving from `before` → `after` WEAKEN a safety-critical clause?

    Returns (is_protected, kind). When is_protected is True the HTTP layer must
    require Super Admin + a governance_reason. `kind` is a short tag for audit /
    notification text.
    """
    before_crit = is_safety_critical(_b(before, "affects_compliance"),
                                     _b(before, "bound_check_key"))
    after_crit = is_safety_critical(_b(after, "affects_compliance"),
                                    _b(after, "bound_check_key"))

    if before_crit:
        # disable
        if _b(before, "is_active", True) and _b(after, "is_active", True) is False:
            return True, "disable"
        # downgrade blocking → advisory
        if _b(before, "rule_type") in _BLOCKING_TYPES and \
           _b(after, "rule_type") in _WEAK_TYPES:
            return True, "downgrade"
        # re-bind to a different check
        if (_b(after, "bound_check_key") or None) != (_b(before, "bound_check_key") or None):
            return True, "rebind"
        # un-bind from compliance entirely
        if _b(before, "affects_compliance") and not _b(after, "affects_compliance"):
            return True, "unbind"

    # Newly binding a critical check with a non-blocking type = sneaking in a
    # weak rule that would override the family. Treat as protected too.
    if after_crit and not before_crit and _b(after, "rule_type") not in _BLOCKING_TYPES:
        return True, "weak_new_binding"

    # Changing the OPERATIONAL PARAMETERS (max_hours, min_rest, turnaround, …) of
    # a safety-critical clause alters what the engine actually enforces — gate it
    # regardless of direction (tighten or loosen).
    if (before_crit or after_crit) and _b(before, "parameters", {}) != _b(after, "parameters", {}):
        return True, "param_change"

    return False, None


def is_super_admin(user: dict) -> bool:
    return user.get("role") == "super_admin" or bool(user.get("is_superuser"))


def gate_decision(user: dict, before: dict, after: dict,
                  reason: str | None) -> tuple[str, str | None]:
    """Combined gate: classify the change, then enforce Super Admin + reason.

    Returns (status, kind):
      • "ok"              — allowed (whether protected or not)
      • "forbidden"       — protected change by a non-Super-Admin
      • "reason_required" — protected change by Super Admin but no reason given
    `kind` is the weakening tag (or None when not protected).
    """
    protected, kind = evaluate_governance_change(before, after)
    if not protected:
        return "ok", kind
    if not is_super_admin(user):
        return "forbidden", kind
    if not (reason or "").strip():
        return "reason_required", kind
    return "ok", kind


_KIND_AR = {
    "disable":          "تعطيل",
    "downgrade":        "تخفيض الشدّة (حاجب → تنبيه)",
    "rebind":           "تغيير الفحص المربوط",
    "unbind":           "إلغاء الربط بالامتثال",
    "weak_new_binding": "ربط فحص حرج بقاعدة غير حاجبة",
    "param_change":     "تغيير القيم التشغيلية",
}
_KIND_EN = {
    "disable":          "disabled",
    "downgrade":        "downgraded (blocking → warning)",
    "rebind":           "re-bound to a different check",
    "unbind":           "un-bound from compliance",
    "weak_new_binding": "bound a critical check to a non-blocking rule",
    "param_change":     "operational parameters changed",
}


def governance_notification(article_id: str, changed_by_name: str,
                            kind: str | None, reason: str) -> dict:
    """Build the title/message a management notification carries."""
    kar = _KIND_AR.get(kind or "", "تغيير حرج")
    ken = _KIND_EN.get(kind or "", "critical change")
    return {
        "title_ar": "تغيير على بند OM حرج",
        "title_en": "Safety-critical OM clause changed",
        "message_ar": f"تم {kar} للبند {article_id} بواسطة {changed_by_name}. السبب: {reason}",
        "message_en": f"Clause {article_id} {ken} by {changed_by_name}. Reason: {reason}",
    }
