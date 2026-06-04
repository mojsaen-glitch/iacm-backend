import os, re, uuid, math, secrets, string, logging
from typing import Optional
from datetime import datetime, timezone, date
from fastapi import APIRouter, Query, HTTPException
from app.api.deps import SbClient, CurrentUser
from app.core.exceptions import NotFoundError, ConflictError, ForbiddenError
from app.core.config import settings
from app.core.security import get_password_hash
from app.api.v1.endpoints.assignments import (
    SCHED_ALLOWED_ROLES, ALLOC_ALLOWED_CATEGORIES,
)
from app.core.crew_roles import roles_in_categories, expand_with_legacy, normalize_role


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

    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)

    # Guard roster-name uniqueness on edit too (excluding this same record).
    if "roster_name" in data:
        _ensure_roster_name_unique(
            sb, current_user["company_id"], data.get("roster_name", ""),
            exclude_crew_id=crew_id,
        )

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
    """Delete a crew member. Admin only."""
    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")
    existing = sb.table("crew").select("id").eq("id", crew_id).eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise NotFoundError("Crew member", crew_id)
    # Remove assignments first to avoid FK violations
    sb.table("assignments").delete().eq("crew_id", crew_id).execute()
    sb.table("crew").delete().eq("id", crew_id).execute()


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
    assign_res = sb.table("assignments").select("flight_id")\
        .eq("crew_id", crew_id).order("created_at", desc=True).limit(2000).execute()
    flight_ids = [r["flight_id"] for r in (assign_res.data or []) if r.get("flight_id")]

    if not flight_ids:
        return []

    today = date.today().isoformat()
    # Chunk the IN() filter (PostgREST caps the list length) and merge the pages.
    rows: list = []
    for i in range(0, len(flight_ids), 500):
        res = sb.table("flights").select("*")\
            .in_("id", flight_ids[i:i + 500])\
            .eq("company_id", current_user["company_id"])\
            .neq("status", "cancelled")\
            .gte("departure_time", today)\
            .order("departure_time", desc=False).execute()
        rows.extend(res.data or [])
    rows.sort(key=lambda f: f.get("departure_time") or "")
    return rows


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
