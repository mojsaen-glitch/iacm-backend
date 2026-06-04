from fastapi import APIRouter, HTTPException, Request
from datetime import datetime, timezone
from typing import List
from pydantic import BaseModel
from app.api.deps import SbClient, CurrentUser, AdminOnly
from app.schemas.auth import (
    LoginRequest, TokenResponse, RefreshTokenRequest,
    CurrentUserResponse, CreateUserRequest, UserListItem,
    ChangePasswordRequest,
)
from app.core.security import verify_password, get_password_hash, create_access_token, create_refresh_token, decode_token
from app.core.exceptions import UnauthorizedError, ForbiddenError
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.departments import is_global_admin, managed_roles_for

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _ensure_user_manager(current_user: dict) -> set | None:
    """Authorize user-management actions.

    Returns None for global admins (may manage ANY role), or the set of roles a
    department admin may manage. Raises ForbiddenError if neither.
    """
    role = current_user.get("role")
    if is_global_admin(role):
        return None
    managed = managed_roles_for(role)
    if managed is None:
        raise ForbiddenError("Admin access required")
    return managed


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(request: Request, data: LoginRequest, sb: SbClient):
    """Password login. If 2FA is enabled for the account, the response is
    `{requires_2fa: true, challenge_token: ...}` (HTTP 200, NO access token);
    the client must follow up with POST /auth/2fa/login carrying the
    challenge_token + the 6-digit code."""
    result = sb.table("users").select("*").eq("email", data.email).eq("is_active", True).execute()
    users = result.data
    if not users:
        raise UnauthorizedError("Invalid email or password")
    user = users[0]
    if not verify_password(data.password, user["hashed_password"]):
        raise UnauthorizedError("Invalid email or password")

    # 2FA gate — if enrolled, return a short-lived challenge token instead
    # of the real access token. The actual access token is minted only
    # after the user posts a valid code to /auth/2fa/login.
    if user.get("totp_enabled"):
        challenge = create_access_token(
            subject=user["id"],
            extra_claims={"purpose": "2fa_challenge"},
            expires_minutes=5,
        )
        # NOTE: returned as a plain dict (not TokenResponse) so the client
        # can branch on `requires_2fa` without crashing on a missing token.
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "requires_2fa":   True,
            "challenge_token": challenge,
        })

    access_token = create_access_token(subject=user["id"])
    refresh_token = create_refresh_token(subject=user["id"])

    sb.table("users").update({
        "refresh_token": refresh_token,
        "last_login": datetime.now(timezone.utc).isoformat(),
    }).eq("id", user["id"]).execute()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ── 2FA TOTP (M2+) ─────────────────────────────────────────────────────
class _TwoFactorLoginRequest(BaseModel):
    challenge_token: str
    code:            str


@router.post("/2fa/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login_2fa(request: Request, data: _TwoFactorLoginRequest, sb: SbClient):
    """Second step of 2FA login — exchange (challenge_token + code) for an
    access+refresh pair. The challenge token expires after 5 minutes."""
    payload = decode_token(data.challenge_token)
    if not payload or payload.get("purpose") != "2fa_challenge":
        raise UnauthorizedError("Invalid or expired challenge")
    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Invalid challenge token")
    res = sb.table("users").select("*").eq("id", user_id) \
        .eq("is_active", True).execute()
    if not res.data:
        raise UnauthorizedError("User not found")
    user = res.data[0]
    secret = user.get("totp_secret")
    if not secret or not user.get("totp_enabled"):
        raise UnauthorizedError("2FA is not enabled for this account")
    import pyotp
    if not pyotp.TOTP(secret).verify(data.code.strip(), valid_window=1):
        raise UnauthorizedError("Invalid 2FA code")
    access_token  = create_access_token(subject=user["id"])
    refresh_token = create_refresh_token(subject=user["id"])
    sb.table("users").update({
        "refresh_token": refresh_token,
        "last_login":    datetime.now(timezone.utc).isoformat(),
    }).eq("id", user["id"]).execute()
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/2fa/enroll")
async def enroll_2fa(current_user: CurrentUser, sb: SbClient):
    """Begin 2FA enrolment — generates + persists a TOTP secret and returns
    the otpauth:// URI the client renders as a QR code. The user must then
    verify a code via /auth/2fa/verify to flip `totp_enabled=true`."""
    import pyotp
    secret = pyotp.random_base32()
    sb.table("users").update({
        "totp_secret":  secret,
        "totp_enabled": False,    # not active until verified
    }).eq("id", current_user["id"]).execute()
    email = current_user.get("email") or "user"
    issuer = "IACM Admin"
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)
    return {"secret": secret, "otpauth_uri": uri, "issuer": issuer}


class _Verify2FARequest(BaseModel):
    code: str


@router.post("/2fa/verify")
async def verify_2fa(data: _Verify2FARequest, current_user: CurrentUser, sb: SbClient):
    """Finish enrolment — verify that the scanned secret produces a valid
    code. On success, flip totp_enabled=true."""
    res = sb.table("users").select("totp_secret").eq("id", current_user["id"]).execute()
    if not res.data or not res.data[0].get("totp_secret"):
        raise HTTPException(status_code=400, detail="ابدأ التسجيل أولاً عبر /auth/2fa/enroll")
    secret = res.data[0]["totp_secret"]
    import pyotp
    if not pyotp.TOTP(secret).verify(data.code.strip(), valid_window=1):
        raise UnauthorizedError("Invalid 2FA code")
    sb.table("users").update({
        "totp_enabled":     True,
        "totp_enrolled_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", current_user["id"]).execute()
    return {"ok": True, "totp_enabled": True}


@router.post("/2fa/disable")
async def disable_2fa(data: _Verify2FARequest, current_user: CurrentUser, sb: SbClient):
    """Disable 2FA — requires a valid current code so a stolen JWT alone
    can't turn it off."""
    res = sb.table("users").select("totp_secret,totp_enabled") \
        .eq("id", current_user["id"]).execute()
    if not res.data or not res.data[0].get("totp_enabled"):
        return {"ok": True, "totp_enabled": False}
    secret = res.data[0]["totp_secret"]
    import pyotp
    if not pyotp.TOTP(secret).verify(data.code.strip(), valid_window=1):
        raise UnauthorizedError("Invalid 2FA code")
    sb.table("users").update({
        "totp_secret":  None,
        "totp_enabled": False,
    }).eq("id", current_user["id"]).execute()
    return {"ok": True, "totp_enabled": False}


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("5/minute")
async def refresh_token(request: Request, data: RefreshTokenRequest, sb: SbClient):
    payload = decode_token(data.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise UnauthorizedError("Invalid or expired refresh token")

    result = sb.table("users").select("*").eq("refresh_token", data.refresh_token).eq("is_active", True).execute()
    if not result.data:
        raise UnauthorizedError("Invalid refresh token")
    user = result.data[0]

    new_access = create_access_token(subject=user["id"])
    new_refresh = create_refresh_token(subject=user["id"])
    sb.table("users").update({"refresh_token": new_refresh}).eq("id", user["id"]).execute()

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout")
async def logout(current_user: CurrentUser, sb: SbClient):
    sb.table("users").update({"refresh_token": None}).eq("id", current_user["id"]).execute()
    return {"message": "Logged out successfully"}


@router.post("/change-password")
@limiter.limit("5/minute")
async def change_password(request: Request, data: ChangePasswordRequest, current_user: CurrentUser, sb: SbClient):
    """Allow authenticated user to change their own password."""
    if len(data.new_password) < 8:
        raise HTTPException(status_code=422, detail="كلمة المرور يجب أن تكون 8 أحرف على الأقل")

    # Re-fetch user with hashed_password
    result = sb.table("users").select("hashed_password").eq("id", current_user["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(data.old_password, result.data[0]["hashed_password"]):
        raise HTTPException(status_code=400, detail="كلمة المرور الحالية غير صحيحة")

    sb.table("users").update({
        "hashed_password": get_password_hash(data.new_password),
    }).eq("id", current_user["id"]).execute()

    return {"message": "Password changed successfully"}


@router.get("/me", response_model=CurrentUserResponse)
async def get_me(current_user: CurrentUser):
    return CurrentUserResponse(
        id=current_user["id"],
        email=current_user["email"],
        name_ar=current_user["name_ar"],
        name_en=current_user["name_en"],
        role=current_user["role"],
        crew_department=current_user.get("crew_department"),
        company_id=current_user["company_id"],
        crew_id=current_user.get("crew_id"),
        is_active=current_user["is_active"],
        avatar_path=current_user.get("avatar_path"),
    )


# ── User Management (admin only) ──────────────────────────────

@router.get("/users", response_model=List[UserListItem])
async def list_users(current_user: CurrentUser, sb: SbClient):
    """List users in the same company. Global admins see everyone; a department
    admin sees only the staff whose role belongs to their department."""
    managed = _ensure_user_manager(current_user)

    result = sb.table("users") \
        .select("id,email,name_ar,name_en,role,is_active,company_id,last_login") \
        .eq("company_id", current_user["company_id"]) \
        .order("name_ar") \
        .execute()

    rows = result.data or []
    if managed is not None:
        rows = [u for u in rows if u.get("role") in managed]
    return [UserListItem(**u) for u in rows]


@router.post("/users", response_model=UserListItem, status_code=201)
async def create_user(data: CreateUserRequest, current_user: CurrentUser, sb: SbClient):
    """Create a new system user. Global admins create any role; a department
    admin may only create staff within their own department."""
    managed = _ensure_user_manager(current_user)
    if managed is not None and data.role not in managed:
        raise ForbiddenError("لا يمكنك إنشاء حساب بدور خارج شعبتك")

    # Check email uniqueness
    existing = sb.table("users").select("id").eq("email", data.email).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="البريد الإلكتروني مستخدم بالفعل")

    company_id = data.company_id or current_user["company_id"]

    # Auto-assign crew_department based on role
    crew_department = data.crew_department
    if not crew_department:
        if data.role == "cabin_allocator":
            crew_department = "cabin"
        elif data.role == "cockpit_allocator":
            crew_department = "cockpit"
        elif data.role == "ground_allocator":
            crew_department = "ground"

    new_user = {
        "email": data.email,
        "hashed_password": get_password_hash(data.password),
        "name_ar": data.name_ar,
        "name_en": data.name_en,
        "role": data.role,
        "crew_department": crew_department,
        "company_id": company_id,
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    result = sb.table("users").insert(new_user).execute()
    created = result.data[0]

    return UserListItem(
        id=created["id"],
        email=created["email"],
        name_ar=created["name_ar"],
        name_en=created["name_en"],
        role=created["role"],
        is_active=created["is_active"],
        company_id=created["company_id"],
        last_login=created.get("last_login"),
    )


@router.patch("/users/{user_id}/role", response_model=UserListItem)
async def update_user_role(user_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Change a user's role. Cannot demote yourself. Department admins may only
    move staff between roles WITHIN their own department."""
    managed = _ensure_user_manager(current_user)
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="لا يمكنك تعديل دور حسابك الخاص")

    new_role = (data.get("role") or "").strip()
    if not new_role:
        raise HTTPException(status_code=422, detail="role is required")

    # Locking down super_admin promotion to existing super_admins only
    if new_role == "super_admin" and current_user["role"] != "super_admin":
        raise ForbiddenError("Only a super admin can grant super_admin")

    existing = sb.table("users").select("*").eq("id", user_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    # Department admins: both the target's current role and the new role must
    # be inside their department.
    if managed is not None and (
        existing.data[0].get("role") not in managed or new_role not in managed
    ):
        raise ForbiddenError("لا يمكنك تعديل دور خارج شعبتك")

    updated = sb.table("users").update({"role": new_role}).eq("id", user_id).execute()
    u = updated.data[0]
    return UserListItem(
        id=u["id"], email=u["email"], name_ar=u["name_ar"], name_en=u["name_en"],
        role=u["role"], is_active=u["is_active"], company_id=u["company_id"],
        last_login=u.get("last_login"),
    )


@router.patch("/users/{user_id}/toggle", response_model=UserListItem)
async def toggle_user_active(user_id: str, current_user: CurrentUser, sb: SbClient):
    """Activate or deactivate a user. Department admins only within their dept."""
    managed = _ensure_user_manager(current_user)

    # Cannot deactivate self
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="لا يمكنك تعطيل حسابك الخاص")

    result = sb.table("users").select("*").eq("id", user_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    user = result.data[0]
    if managed is not None and user.get("role") not in managed:
        raise ForbiddenError("لا يمكنك تعطيل مستخدم خارج شعبتك")
    new_status = not user["is_active"]
    updated = sb.table("users").update({"is_active": new_status}).eq("id", user_id).execute()
    u = updated.data[0]

    return UserListItem(
        id=u["id"], email=u["email"], name_ar=u["name_ar"], name_en=u["name_en"],
        role=u["role"], is_active=u["is_active"], company_id=u["company_id"],
        last_login=u.get("last_login"),
    )


@router.post("/users/{user_id}/reset-password")
@limiter.limit("3/minute")
async def reset_user_password(request: Request, user_id: str, data: dict, current_user: CurrentUser, sb: SbClient):
    """Admin resets another user's password. The temp password is shown ONCE in the
    response so the admin can communicate it through a secure channel (do NOT log it).
    Refresh tokens are invalidated to force re-login."""
    managed = _ensure_user_manager(current_user)

    # Admin cannot reset their own password through this endpoint (use /change-password)
    if user_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="استخدم تغيير كلمة المرور لحسابك الخاص")

    result = sb.table("users").select("id,email,name_ar,name_en,role").eq("id", user_id)\
        .eq("company_id", current_user["company_id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    if managed is not None and result.data[0].get("role") not in managed:
        raise ForbiddenError("لا يمكنك إعادة تعيين كلمة مرور مستخدم خارج شعبتك")

    new_password = (data.get("new_password") or "").strip()
    if not new_password:
        import secrets, string
        alphabet     = string.ascii_letters + string.digits + "!@#$%"
        new_password = "".join(secrets.choice(alphabet) for _ in range(16))

    if len(new_password) < 8:
        raise HTTPException(status_code=422, detail="كلمة المرور يجب أن تكون 8 أحرف على الأقل")

    sb.table("users").update({
        "hashed_password": get_password_hash(new_password),
        "refresh_token":   None,
    }).eq("id", user_id).execute()

    # Audit trail — never log the password itself
    try:
        admin_name = current_user.get("name_ar") or current_user.get("name_en") or current_user["email"]
        sb.table("audit_log").insert({
            "user_id":     current_user["id"],
            "user_name":   admin_name,
            "action":      "reset_password",
            "entity_type": "user",
            "entity_id":   user_id,
            "ip_address":  request.client.host if request.client else None,
            "company_id":  current_user["company_id"],
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        # Audit failure must not break the operation, but it should be visible in server logs
        import logging
        logging.getLogger(__name__).exception("Failed to write audit log for password reset")

    return {
        "message":       "تم إعادة تعيين كلمة المرور — أبلغ المستخدم عبر قناة آمنة",
        "temp_password": new_password,
        "email":         result.data[0]["email"],
    }
