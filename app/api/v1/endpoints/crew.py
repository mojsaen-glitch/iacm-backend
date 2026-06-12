import os, re, json, uuid, math, secrets, string, logging
from typing import Optional
from datetime import datetime, timezone, date
from fastapi import APIRouter, Query, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.core.audit import write_audit
from app.core.exceptions import NotFoundError, ConflictError, ForbiddenError
from app.core.config import settings
from app.core.security import get_password_hash
from app.api.v1.endpoints.assignments import (
    SCHED_ALLOWED_ROLES, ALLOC_ALLOWED_CATEGORIES,
)
from app.core.crew_roles import roles_in_categories, expand_with_legacy, normalize_role
from app.core.monthly_hours import crew_flight_hours


def _restricted_ranks(user: dict) -> Optional[set]:
    """The crew ROLES this user is limited to see/manage, or None for all.

    Specialty schedulers → their exact GenDec roles; broad allocators → every
    role in their allowed categories — so the roster never reveals other-
    department crew. Admin / ops_manager / scheduler / crew_allocator see all.
    The set is expanded with legacy crew.rank values so the DB filter matches
    BOTH old and new stored ranks.
    """
    role = user.get("role", "")
    sched = SCHED_ALLOWED_ROLES.get(role)
    if sched is not None:
        return expand_with_legacy(sched)
    cats = ALLOC_ALLOWED_CATEGORIES.get(role)
    if cats is not None:
        return expand_with_legacy(roles_in_categories(cats))
    return None


def _generate_temp_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(secrets.choice(alphabet) for _ in range(12))


# ── Contact phones (primary required, secondary optional) ────────────────────
# Numbers only (a single leading '+' allowed); supports Iraqi 07xxxxxxxxx and
# +9647xxxxxxxxx. Spaces/dashes are stripped before validation/storage.
def _normalize_phone(value) -> str:
    return re.sub(r"[\s\-]", "", str(value or "").strip())


def _valid_phone(p: str) -> bool:
    return bool(re.fullmatch(r"\+?[0-9]{7,15}", p))


def _mask_phone(p) -> str:
    """Hide the middle of a number for the audit log, e.g. 07******678."""
    s = str(p or "")
    if len(s) <= 5:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 5) + s[-3:]

router = APIRouter(prefix="/crew", tags=["Crew Management"])

# Role gates — kept here so anyone scanning this file sees the policy at a
# glance. Update both `_READERS` and the AuthProvider helpers in lock-step.
_READERS = {
    "super_admin", "admin", "ops_manager", "scheduler",
    "crew_allocator", "cabin_allocator", "cockpit_allocator", "ground_allocator",
    "compliance_officer", "flight_movement", "flight_ops", "flight_operations",
    # Specialty schedulers
    "sched_captain", "sched_copilot", "sched_engineer", "sched_purser",
    "sched_cabin", "sched_balance", "sched_security", "sched_extra",
}
_EDITORS = {"super_admin", "admin", "ops_manager", "scheduler"}


def _ensure_reader(user: dict) -> None:
    if user.get("role") not in _READERS:
        raise ForbiddenError("Only operations staff can browse the crew roster")


def _ensure_editor(user: dict) -> None:
    if user.get("role") not in _EDITORS:
        raise ForbiddenError("Only admin / ops manager / scheduler can edit crew records")


def _is_own_record(user: dict, crew_id: str) -> bool:
    """True when a logged-in crew member is reading/editing their own row."""
    return user.get("role") == "crew" and user.get("crew_id") == crew_id


def _ensure_roster_name_unique(sb, company_id: str, roster_name: str,
                               exclude_crew_id: Optional[str] = None) -> None:
    """Reject a roster short-name already used by another crew member in the
    same company (case-insensitive). NULL/empty names are never checked, so
    crew without a roster name don't collide with each other."""
    name = (roster_name or "").strip()
    if not name:
        return
    q = sb.table("crew").select("id").eq("company_id", company_id).ilike("roster_name", name)
    res = q.execute()
    for row in (res.data or []):
        if exclude_crew_id and row.get("id") == exclude_crew_id:
            continue
        raise ConflictError(f"اسم الروستر '{name}' مستخدم مسبقاً — اختر اسماً آخر")


@router.get("/roster-name/available")
async def roster_name_available(
    current_user: CurrentUser,
    sb: SbClient,
    name: str = Query(..., min_length=1),
    exclude_id: Optional[str] = Query(None),
):
    """Live check for the add/edit crew form: is this roster short-name free in
    the caller's company? Mirrors _ensure_roster_name_unique (case-insensitive)
    but returns a flag instead of raising. Defined BEFORE /{crew_id} so the
    literal path wins routing."""
    _ensure_editor(current_user)
    n = (name or "").strip()
    if not n:
        return {"available": True, "taken": False}
    res = sb.table("crew").select("id") \
        .eq("company_id", current_user["company_id"]) \
        .ilike("roster_name", n).execute()
    taken = any(
        not (exclude_id and r.get("id") == exclude_id)
        for r in (res.data or [])
    )
    return {"available": not taken, "taken": taken}


@router.get("")
async def list_crew(
    current_user: CurrentUser,
    sb: SbClient,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    status: Optional[str] = None,
    rank: Optional[str] = None,
    search: Optional[str] = None,
    operator_company_id: Optional[str] = None,
):
    _ensure_reader(current_user)
    # `estimated` count uses the Postgres planner statistics for large tables
    # (no full count scan) and falls back to exact for small ones — keeps the
    # roster fetch fast even with thousands of rows. Total is used only for
    # page count, so a planner estimate is acceptable.
    query = sb.table("crew").select("*", count="estimated").eq("company_id", current_user["company_id"])
    # Specialty schedulers / department allocators see ONLY their own ranks —
    # other departments' crew identities are never returned to them.
    restricted = _restricted_ranks(current_user)
    if restricted is not None:
        query = query.in_("rank", list(restricted))
    if status:
        query = query.eq("status", status)
    if operator_company_id and operator_company_id != "all":
        query = query.eq("operator_company_id", operator_company_id)
    if rank:
        # Expand the requested rank with its legacy aliases so a query for
        # `load_sheet_officer` (new GenDec key) also matches rows stored with
        # the old shorthand (`balance`, `dispatcher`, `ground_staff`, …).
        rank_set = expand_with_legacy({rank})
        # An explicit rank filter must stay within the user's allowed set.
        if restricted is not None and not (rank_set & restricted):
            return {"items": [], "total": 0, "page": page,
                    "page_size": page_size, "total_pages": 1}
        query = query.in_("rank", list(rank_set))
    if search:
        safe = re.sub(r"[^a-zA-Z0-9؀-ۿ\s\-]", "", search.strip())[:100]
        query = query.or_(f"full_name_ar.ilike.%{safe}%,full_name_en.ilike.%{safe}%,employee_id.ilike.%{safe}%")

    skip = (page - 1) * page_size
    result = query.range(skip, skip + page_size - 1).execute()
    total = result.count or 0

    return {
        "items": result.data,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
    }


# Roles allowed to create crew. Specialty schedulers may create crew too, but
# ONLY of the rank(s) their specialty manages (enforced below) — matching the
# "each scheduler manages their own rank" policy used for assignments.
_CREW_CREATORS = {"super_admin", "admin", "ops_manager", "scheduler_admin"}
# Canonical GenDec role each specialty scheduler may CREATE (compared after
# normalize_role(), so the old legacy values — captain / dispatcher / balance /
# security / extra — are accepted too).
_SCHED_CREATE_RANKS = {
    "sched_captain":  {"pilot_captain"},
    "sched_copilot":  {"pilot_first_officer"},
    "sched_engineer": {"aircraft_maintenance_engineer", "technical_staff"},
    "sched_purser":   {"senior_cabin_crew"},
    "sched_cabin":    {"cabin_crew"},
    "sched_balance":  {"load_sheet_officer"},
    "sched_security": {"in_flight_security_officer", "security_staff"},
    "sched_extra":    {"observer"},
}


@router.post("", status_code=201)
async def create_crew(data: dict, current_user: CurrentUser, sb: SbClient):
    role = current_user.get("role")
    sched_ranks = _SCHED_CREATE_RANKS.get(role)
    if role not in _CREW_CREATORS and sched_ranks is None and not current_user.get("is_superuser"):
        raise ForbiddenError("Insufficient permissions")

    # A specialty scheduler may only create crew of the rank(s) they manage.
    # Compare on the normalised (canonical GenDec) key so both the new value
    # (load_sheet_officer) and any legacy shorthand (balance, dispatcher, …)
    # the frontend might still send are accepted equivalently.
    if sched_ranks is not None:
        requested_rank = (data.get("rank") or "").strip()
        if requested_rank and normalize_role(requested_rank) not in sched_ranks:
            raise ForbiddenError("يمكنك إنشاء طاقم من رتبتك فقط")
        if not requested_rank:
            data["rank"] = next(iter(sched_ranks))

    employee_id = data.get("employee_id", "").strip()
    if not employee_id:
        raise HTTPException(status_code=422, detail="employee_id is required")
    existing = sb.table("crew").select("id").eq("employee_id", employee_id).execute()
    if existing.data:
        raise ConflictError(f"Employee ID '{employee_id}' already exists")

    # Roster short-name must be unique across the company (cabin or cockpit).
    _ensure_roster_name_unique(sb, current_user["company_id"], data.get("roster_name", ""))

    # Every crew member must belong to an operator airline (operator_company_id).
    if not (data.get("operator_company_id") or "").strip():
        raise HTTPException(status_code=422, detail="operator_company_id (الشركة) is required")

    # ── Contact phones: primary required + valid; secondary optional + valid.
    # Legacy `phone` column is kept in sync with the primary so older readers
    # don't break. (Falls back to a legacy `phone` value if the client still
    # sends that key instead of primary_phone.)
    primary = _normalize_phone(data.get("primary_phone") or data.get("phone"))
    if not primary:
        raise HTTPException(status_code=422, detail="رقم الهاتف الأساسي مطلوب")
    if not _valid_phone(primary):
        raise HTTPException(status_code=422, detail="صيغة رقم الهاتف الأساسي غير صحيحة")
    data["primary_phone"] = primary
    data["phone"] = primary
    sec = _normalize_phone(data.get("secondary_phone"))
    if sec and not _valid_phone(sec):
        raise HTTPException(status_code=422, detail="صيغة رقم الهاتف البديل غير صحيحة")
    data["secondary_phone"] = sec or None

    crew_id = str(uuid.uuid4())
    data["id"] = crew_id
    data["company_id"] = current_user["company_id"]
    data.setdefault("status", "active")
    data.setdefault("monthly_flight_hours", 0)
    data.setdefault("total_flight_hours", 0)
    data.setdefault("max_monthly_hours", 100)
    # `base` is NOT NULL in the schema — default to BGW (Baghdad) so the form
    # doesn't need to send it. Operators can override later from the profile.
    data.setdefault("base", "BGW")
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Strip empty-string values for not-null/typed columns so Postgres receives
    # nulls only for genuinely-optional fields.
    for k in list(data.keys()):
        if data[k] == "":
            data[k] = None

    try:
        result = sb.table("crew").insert(data).execute()
    except Exception as e:
        # Surface the real Postgres error instead of a generic 500 — the
        # frontend can then show it to the user (e.g. missing required field,
        # bad data type, FK violation, duplicate).
        msg = str(e)
        if "null value in column" in msg:
            # extract the column name for a friendlier message
            import re as _re
            m = _re.search(r'column \"(\w+)\"', msg)
            col = m.group(1) if m else "(unknown)"
            raise HTTPException(
                status_code=422,
                detail=f"حقل مطلوب مفقود: {col}",
            )
        raise HTTPException(status_code=502, detail=f"تعذّر إنشاء الطاقم: {msg[:200]}")
    crew = result.data[0] if result.data else {}

    # ── Auto-create login account ──────────────────────────────────────────
    account_info = {"email": None, "password": None, "account_created": False}
    if employee_id:
        # Sanitize employee_id for use as the local-part of an email
        import re as _re
        local_part = _re.sub(r"[^a-z0-9._-]+", "", employee_id.lower())
        if not local_part:
            account_info = {"email": None, "account_created": False,
                            "skipped_reason": "invalid_employee_id"}
        else:
            email = f"{local_part}@iraqiairways.iq"
            # Validate format defensively
            if not _re.fullmatch(r"[a-z0-9._-]+@[a-z0-9.-]+\.[a-z]{2,}", email):
                account_info = {"email": None, "account_created": False,
                                "skipped_reason": "invalid_email_format"}
            else:
                password = _generate_temp_password()
                # Only create if no account exists yet
                existing_user = sb.table("users").select("id").eq("email", email).execute()
                if not existing_user.data:
                    sb.table("users").insert({
                        "id":              str(uuid.uuid4()),
                        "email":           email,
                        "hashed_password": get_password_hash(password),
                        "name_ar":         data.get("full_name_ar", ""),
                        "name_en":         data.get("full_name_en", ""),
                        "role":            "crew",
                        "company_id":      current_user["company_id"],
                        "crew_id":         crew_id,
                        "is_active":       True,
                        "created_at":      datetime.now(timezone.utc).isoformat(),
                    }).execute()
                    # Return the temp password ONCE so admin can communicate it to the crew member
                    account_info = {"email": email, "temp_password": password, "account_created": True}
                else:
                    account_info = {"email": email, "account_created": False}

    crew["account"] = account_info
    return crew


@router.get("/{crew_id}")
async def get_crew(crew_id: str, current_user: CurrentUser, sb: SbClient):
    # Readers OR the crew member themself (so a pilot can open /crew-portal
    # and load their own profile).
    if not _is_own_record(current_user, crew_id):
        _ensure_reader(current_user)
    result = sb.table("crew").select("*").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not result.data:
        raise NotFoundError("Crew member", crew_id)
    return result.data[0]


@router.patch("/{crew_id}")
async def update_crew(crew_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    # Editors are admin/ops/scheduler. We deliberately do NOT allow a crew
    # member to PATCH their own row — fields like rank, salary, base, etc.
    # are owned by Ops. Crew-driven self-service (e.g. avatar, phone) should
    # go through a dedicated, field-whitelisted endpoint when we add it.
    _ensure_editor(current_user)

    existing = sb.table("crew").select("id, primary_phone, secondary_phone, phone") \
        .eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)
    before = existing.data[0]

    # Guard roster-name uniqueness on edit too (excluding this same record).
    if "roster_name" in data:
        _ensure_roster_name_unique(
            sb, current_user["company_id"], data.get("roster_name", ""),
            exclude_crew_id=crew_id,
        )

    # ── Contact phones: validate when present, keep legacy `phone` synced, and
    # record which phone fields changed (for a masked audit entry after save).
    phone_changed: dict = {}
    if "primary_phone" in data:
        primary = _normalize_phone(data.get("primary_phone"))
        if not primary:
            raise HTTPException(status_code=422, detail="رقم الهاتف الأساسي لا يمكن أن يكون فارغاً")
        if not _valid_phone(primary):
            raise HTTPException(status_code=422, detail="صيغة رقم الهاتف الأساسي غير صحيحة")
        data["primary_phone"] = primary
        data["phone"] = primary  # legacy column stays in sync with the primary
        old_primary = before.get("primary_phone") or before.get("phone") or ""
        if primary != old_primary:
            phone_changed["primary_phone"] = (old_primary, primary)
    if "secondary_phone" in data:
        sec = _normalize_phone(data.get("secondary_phone"))
        if sec and not _valid_phone(sec):
            raise HTTPException(status_code=422, detail="صيغة رقم الهاتف البديل غير صحيحة")
        data["secondary_phone"] = sec or None
        if (sec or None) != (before.get("secondary_phone") or None):
            phone_changed["secondary_phone"] = (before.get("secondary_phone"), sec or None)

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Never overwrite the row's company with a client-supplied value (the form
    # hardcodes a constant company_id which can mismatch this tenant and break
    # the FK).
    for k in ("company_id", "id"):
        data.pop(k, None)
    try:
        result = sb.table("crew").update(data).eq("id", crew_id) \
            .eq("company_id", current_user["company_id"]).execute()
    except Exception as e:
        logging.getLogger(__name__).exception("crew update failed for %s", crew_id)
        raise HTTPException(status_code=502, detail=f"crew update failed: {str(e)[:300]}")

    # ── Audit phone changes (numbers MASKED — sensitive). One entry per field. ──
    for field, (old, new) in phone_changed.items():
        try:
            sb.table("audit_log").insert({
                "user_id": current_user["id"],
                "user_name": current_user.get("name_ar") or current_user.get("name_en")
                             or current_user.get("email", ""),
                "action": "crew_contact_updated",
                "entity_type": "crew",
                "entity_id": crew_id,
                "company_id": current_user["company_id"],
                "before_data": json.dumps({field: _mask_phone(old)}, ensure_ascii=False),
                "after_data": json.dumps({field: _mask_phone(new)}, ensure_ascii=False),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception:
            logging.getLogger(__name__).warning("crew contact audit failed for %s", crew_id)

    return result.data[0] if result.data else {}


@router.put("/{crew_id}")
async def update_crew_put(crew_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """PUT alias for PATCH — accepts full or partial update."""
    return await update_crew(crew_id, data, current_user, sb)


@router.get("/{crew_id}/account")
async def get_crew_account(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Return the user account (email + active state) linked to a crew member.
    Admin / Ops only. Does NOT return the password — use the reset endpoint
    below to generate a new one."""
    if current_user["role"] not in ("super_admin", "admin", "ops_manager"):
        raise ForbiddenError("Admin access required")
    crew = sb.table("crew").select("id").eq("id", crew_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Crew member", crew_id)
    u = sb.table("users").select("id,email,is_active,last_login") \
        .eq("crew_id", crew_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not u.data:
        return {"has_account": False, "crew_id": crew_id}
    user = u.data[0]
    return {
        "has_account": True,
        "user_id":     user["id"],
        "email":       user["email"],
        "is_active":   user["is_active"],
        "last_login":  user.get("last_login"),
    }


@router.post("/{crew_id}/reset-account-password")
async def reset_crew_account_password(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Admin generates a fresh temporary password for the crew's login
    account, returns it ONCE so the admin can hand it to the crew member.
    All existing refresh tokens are invalidated."""
    if current_user["role"] not in ("super_admin", "admin", "ops_manager"):
        raise ForbiddenError("Admin access required")
    crew = sb.table("crew").select("id,full_name_ar,full_name_en") \
        .eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not crew.data:
        raise NotFoundError("Crew member", crew_id)
    u = sb.table("users").select("id,email") \
        .eq("crew_id", crew_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not u.data:
        raise HTTPException(status_code=404, detail="لا يوجد حساب دخول لهذا الطاقم")

    new_password = _generate_temp_password()
    sb.table("users").update({
        "hashed_password": get_password_hash(new_password),
        "refresh_token":   None,
    }).eq("id", u.data[0]["id"]).execute()

    # Audit trail — never log the password itself
    import logging
    try:
        sb.table("audit_log").insert({
            "user_id":     current_user["id"],
            "user_name":   current_user.get("name_ar") or current_user.get("name_en") or current_user["email"],
            "action":      "reset_crew_password",
            "entity_type": "user",
            "entity_id":   u.data[0]["id"],
            "company_id":  current_user["company_id"],
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        logging.getLogger(__name__).exception("audit_log write failed for crew password reset")

    return {
        "message":       "تم إنشاء كلمة مرور جديدة — أبلغ الطاقم عبر قناة آمنة",
        "email":         u.data[0]["email"],
        "temp_password": new_password,
    }


@router.delete("/{crew_id}", status_code=204)
async def delete_crew(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Delete a crew member. Admin only.

    Deleting the member cascades into deleting ALL their assignments — a crew
    member must never silently vanish from a live roster, so each removed
    assignment is audited (reason: crew_deleted), GD-finalised flights are
    marked stale (same hook as remove/replace), and schedulers/ops are alerted
    for every published/finalised flight that lost a member."""
    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")
    cid = current_user["company_id"]
    existing = sb.table("crew").select("id, full_name_ar, full_name_en") \
        .eq("id", crew_id).eq("company_id", cid).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)
    crew_name = existing.data[0].get("full_name_ar") \
        or existing.data[0].get("full_name_en") or crew_id

    # Snapshot the assignments + their flights BEFORE anything is deleted.
    asgs = sb.table("assignments").select("*").eq("crew_id", crew_id) \
        .execute().data or []
    flights_by_id: dict = {}
    fids = [a.get("flight_id") for a in asgs if a.get("flight_id")]
    if fids:
        rows = sb.table("flights").select(
            "id, flight_number, origin_code, destination_code, "
            "publish_status, roster_finalized_status, gd_status") \
            .in_("id", fids).eq("company_id", cid).execute().data or []
        flights_by_id = {r["id"]: r for r in rows}

    # Remove assignments first to avoid FK violations
    sb.table("assignments").delete().eq("crew_id", crew_id).execute()
    sb.table("crew").delete().eq("id", crew_id).execute()

    # ── Protection trail (best-effort — must never fail the delete itself) ──
    log = logging.getLogger(__name__)
    affected_live: list = []
    for a in asgs:
        fl = flights_by_id.get(a.get("flight_id")) or {}
        try:
            sb.table("audit_log").insert({
                "user_id": current_user["id"],
                "user_name": current_user.get("name_ar")
                             or current_user.get("name_en")
                             or current_user.get("email", ""),
                "action": "assignment_removed_by_crew_delete",
                "entity_type": "assignment",
                "entity_id": a.get("id"),
                "company_id": cid,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "after_data": json.dumps({
                    "crew_id": crew_id,
                    "crew_name": crew_name,
                    "flight_id": a.get("flight_id"),
                    "flight_number": fl.get("flight_number"),
                    "assignment_id": a.get("id"),
                    "reason": "crew_deleted",
                    "duty_type": a.get("duty_type"),
                    "assigned_role": a.get("assigned_role"),
                    "publish_status": fl.get("publish_status"),
                    "roster_finalized_status": fl.get("roster_finalized_status"),
                }, ensure_ascii=False),
            }).execute()
        except Exception:
            log.exception("audit failed for crew-delete assignment %s", a.get("id"))
        # Same staleness hook the remove/replace paths use.
        if fl.get("roster_finalized_status") == "finalized":
            try:
                from app.api.v1.endpoints.flights import mark_gd_stale_if_finalized
                mark_gd_stale_if_finalized(sb, cid, fl["id"], actor=current_user)
            except Exception:
                log.exception("gd-stale hook failed for %s", fl.get("id"))
        if fl and (fl.get("publish_status") == "published"
                   or fl.get("roster_finalized_status") == "finalized"):
            affected_live.append(fl)

    # One alert per affected LIVE flight — the roster lost a member.
    for fl in affected_live:
        try:
            from app.api.v1.endpoints.flights import _insert_role_notifications
            fnum = fl.get("flight_number", "")
            o, d = fl.get("origin_code", ""), fl.get("destination_code", "")
            _insert_role_notifications(
                sb, cid,
                ("admin", "super_admin", "ops_manager", "scheduler", "scheduler_admin"),
                "roster_changed",
                f"حذف فرد مكلّف برحلة {fnum}",
                f"Assigned crew deleted — {fnum}",
                f"حُذف {crew_name} من النظام وكان مكلّفاً برحلة {fnum} ({o} → {d}) — "
                f"راجع الجدول وعيّن بديلاً وأعد توليد GD إن كان مولّداً.",
                f"{crew_name} was deleted and held a duty on {fnum} — review the "
                f"roster, assign a replacement, regenerate GD if needed.",
                fl["id"])
        except Exception:
            log.exception("crew-delete alert failed for flight %s", fl.get("id"))


@router.get("/{crew_id}/flights")
async def get_crew_flights(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Return upcoming flights assigned to this crew member."""
    existing = sb.table("crew").select("id").eq("id", crew_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)

    # Flight IDs from this crew's assignments — capped + newest-first (this view
    # only shows UPCOMING flights, so recent assignments are what matter). The cap
    # avoids the Supabase 1000-row truncation on long-career crew.
    # select("*") (column-safe) so the acceptance fields ride along — the crew
    # portal card needs assignment_id/acknowledged/declined to show its
    # موافق/أرفض bar.
    assign_res = sb.table("assignments").select("*")\
        .eq("crew_id", crew_id).order("created_at", desc=True).limit(2000).execute()
    asg_by_fid = {r["flight_id"]: r for r in (assign_res.data or [])
                  if r.get("flight_id")}
    flight_ids = list(asg_by_fid.keys())

    if not flight_ids:
        return []

    today = date.today().isoformat()
    # Chunk the IN() filter (PostgREST caps the list length) and merge the pages.
    rows: list = []
    for i in range(0, len(flight_ids), 500):
        q = sb.table("flights").select("*")\
            .in_("id", flight_ids[i:i + 500])\
            .eq("company_id", current_user["company_id"])\
            .neq("status", "cancelled")\
            .gte("departure_time", today)
        # Crew must NEVER see a DRAFT roster: the duty becomes visible (and the
        # crew is notified) only when the scheduler publishes the flight.
        # Ops/admin viewers still see drafts (they're building them).
        if current_user.get("role") == "crew":
            q = q.eq("publish_status", "published")
        res = q.order("departure_time", desc=False).execute()
        rows.extend(res.data or [])
    rows.sort(key=lambda f: f.get("departure_time") or "")

    # Attach THIS crew's assignment lifecycle to each flight — the portal card
    # renders the موافق/أرفض bar only when assignment_id is present.
    for f in rows:
        a = asg_by_fid.get(f.get("id")) or {}
        f["assignment_id"]   = a.get("id")
        f["acknowledged"]    = bool(a.get("acknowledged"))
        f["acknowledged_at"] = a.get("acknowledged_at")
        f["declined"]        = bool(a.get("declined"))
        f["declined_at"]     = a.get("declined_at")
        f["decline_reason"]  = a.get("decline_reason")
        f["admin_confirmed"] = bool(a.get("admin_confirmed"))
        f["assigned_role"]   = a.get("assigned_role")
        f["duty_type"]       = a.get("duty_type")
    return rows


@router.get("/{crew_id}/flight-hours")
async def get_crew_flight_hours(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Credited flight hours for this crew member, bucketed by period
    (month / last_28_days / year / total). Computed from
    `flights.duration_hours` with the same crediting policy as the Monthly
    Hours report, so the crew profile shows real figures (the stored
    crew.monthly_flight_hours is not maintained)."""
    existing = sb.table("crew").select("id").eq("id", crew_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)
    return crew_flight_hours(sb, current_user["company_id"], crew_id)


@router.post("/{crew_id}/block")
async def block_crew(crew_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    # Blocking a crew member removes them from the assignable pool — must be
    # gated to editors (admin / ops manager / scheduler). Without this gate,
    # any authenticated user (incl. crew themselves) could ground anyone.
    _ensure_editor(current_user)
    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)

    result = sb.table("crew").update({
        "status": "blocked",
        "block_reason": data.get("reason"),
        "blocked_by": current_user["id"],
        "blocked_on": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", crew_id).execute()
    # Blocking grounds a member — first-class governance event.
    write_audit(sb, current_user, "crew_blocked", "crew", crew_id,
                before={"status": "active"},
                after={"status": "blocked"},
                reason=data.get("reason"))
    return result.data[0] if result.data else {}


@router.post("/{crew_id}/unblock")
async def unblock_crew(crew_id: str, current_user: CurrentUser, sb: SbClient):
    _ensure_editor(current_user)
    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)

    result = sb.table("crew").update({
        "status": "active",
        "block_reason": None,
        "blocked_by": None,
        "blocked_on": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", crew_id).execute()
    write_audit(sb, current_user, "crew_unblocked", "crew", crew_id,
                before={"status": "blocked"}, after={"status": "active"})
    return result.data[0] if result.data else {}


@router.post("/{crew_id}/create-account")
async def create_crew_account(crew_id: str, current_user: CurrentUser, sb: SbClient):
    """Auto-create a system login account for a crew member. Admin only."""
    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")

    # Get crew member
    res = sb.table("crew").select("*").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not res.data:
        raise NotFoundError("Crew member", crew_id)
    crew = res.data[0]

    employee_id = crew.get("employee_id", "").strip()
    if not employee_id:
        raise HTTPException(status_code=400, detail="الرقم الوظيفي مطلوب لإنشاء الحساب")

    # Generate credentials
    email = f"{employee_id.lower()}@iraqiairways.iq"
    password = _generate_temp_password()

    # Check if account already exists
    existing = sb.table("users").select("id,email").eq("email", email).execute()
    if existing.data:
        # Return existing account info
        return {"email": email, "password": None, "already_exists": True, "user_id": existing.data[0]["id"]}

    # Create the user account
    new_user = {
        "email": email,
        "hashed_password": get_password_hash(password),
        "name_ar": crew.get("full_name_ar", ""),
        "name_en": crew.get("full_name_en", ""),
        "role": "crew",
        "company_id": current_user["company_id"],
        "crew_id": crew_id,
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = sb.table("users").insert(new_user).execute()
    user = result.data[0] if result.data else {}

    return {
        "email": email,
        "already_exists": False,
        "user_id": user.get("id"),
    }
