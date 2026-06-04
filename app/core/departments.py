"""Department hierarchy — per-division admins who manage their own staff.

Each department has ONE admin role that may create / list / manage only the
member roles of that same department. Global admins (super_admin / admin) keep
unrestricted access. This is the single source of truth used by the auth
endpoints to scope user management.
"""

# department key → admin role + the member roles that admin governs
DEPARTMENTS: dict[str, dict] = {
    "flight_movement": {
        "name_ar": "شعبة الحركة",
        "name_en": "Flight Movement",
        "admin_role": "flight_movement_admin",
        "member_roles": {"flight_movement"},
    },
    "scheduling": {
        "name_ar": "المجدولون",
        "name_en": "Scheduling",
        "admin_role": "scheduler_admin",
        "member_roles": {
            "scheduler", "crew_allocator", "cabin_allocator",
            "cockpit_allocator", "ground_allocator",
            "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
            "sched_cabin", "sched_balance", "sched_security", "sched_extra",
        },
    },
    "flight_operations": {
        "name_ar": "عمليات الطيران",
        "name_en": "Flight Operations",
        "admin_role": "flight_operations_admin",
        "member_roles": {"flight_operations", "flight_ops"},
    },
    "compliance": {
        "name_ar": "الامتثال",
        "name_en": "Compliance",
        "admin_role": "compliance_admin",
        "member_roles": {"compliance_officer"},
    },
}

GLOBAL_ADMINS = {"super_admin", "admin"}

# admin_role → set of roles it may create/manage. A department admin manages its
# member roles (but NOT other department admins — only global admins do that).
_ADMIN_TO_MANAGED: dict[str, set] = {
    d["admin_role"]: set(d["member_roles"]) for d in DEPARTMENTS.values()
}

ALL_DEPT_ADMIN_ROLES = set(_ADMIN_TO_MANAGED.keys())


def is_global_admin(role: str | None) -> bool:
    return role in GLOBAL_ADMINS


def managed_roles_for(role: str | None) -> set | None:
    """Roles a department admin may create/manage, or None if not a dept admin."""
    if not role:
        return None
    return _ADMIN_TO_MANAGED.get(role)
