from fastapi import APIRouter
from datetime import datetime, timezone
from typing import List
from app.api.deps import SbClient, CurrentUser, AdminOnly
from app.schemas.auth import (
    LoginRequest, TokenResponse, RefreshTokenRequest,
    CurrentUserResponse, CreateUserRequest, UserListItem,
)
from app.core.security import verify_password, get_password_hash, create_access_token, create_refresh_token, decode_token
from app.core.exceptions import UnauthorizedError, ForbiddenError
from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, sb: SbClient):
    result = sb.table("users").select("*").eq("email", data.email).eq("is_active", True).execute()
    users = result.data
    if not users:
        raise UnauthorizedError("Invalid email or password")
    user = users[0]
    if not verify_password(data.password, user["hashed_password"]):
        raise UnauthorizedError("Invalid email or password")

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


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(data: RefreshTokenRequest, sb: SbClient):
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
async def change_password(payload: dict, current_user: CurrentUser, sb: SbClient):
    """Allow authenticated user to change their own password."""
    old_password = payload.get("old_password", "")
    new_password = payload.get("new_password", "")

    if not old_password or not new_password:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="old_password and new_password are required")

    if len(new_password) < 6:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="كلمة المرور يجب أن تكون 6 أحرف على الأقل")

    # Re-fetch user with hashed_password
    result = sb.table("users").select("hashed_password").eq("id", current_user["id"]).execute()
    if not result.data:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(old_password, result.data[0]["hashed_password"]):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="كلمة المرور الحالية غير صحيحة")

    sb.table("users").update({
        "hashed_password": get_password_hash(new_password),
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
    """List all users in the same company. Admin/super_admin only."""
    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")

    result = sb.table("users") \
        .select("id,email,name_ar,name_en,role,is_active,company_id,last_login") \
        .eq("company_id", current_user["company_id"]) \
        .order("name_ar") \
        .execute()

    return [UserListItem(**u) for u in result.data]


@router.post("/users", response_model=UserListItem, status_code=201)
async def create_user(data: CreateUserRequest, current_user: CurrentUser, sb: SbClient):
    """Create a new system user. Admin/super_admin only."""
    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")

    # Check email uniqueness
    existing = sb.table("users").select("id").eq("email", data.email).execute()
    if existing.data:
        from fastapi import HTTPException
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


@router.patch("/users/{user_id}/toggle", response_model=UserListItem)
async def toggle_user_active(user_id: str, current_user: CurrentUser, sb: SbClient):
    """Activate or deactivate a user. Admin only."""
    if current_user["role"] not in ("super_admin", "admin"):
        raise ForbiddenError("Admin access required")

    # Cannot deactivate self
    if user_id == current_user["id"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="لا يمكنك تعطيل حسابك الخاص")

    result = sb.table("users").select("*").eq("id", user_id) \
        .eq("company_id", current_user["company_id"]).execute()
    if not result.data:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    user = result.data[0]
    new_status = not user["is_active"]
    updated = sb.table("users").update({"is_active": new_status}).eq("id", user_id).execute()
    u = updated.data[0]

    return UserListItem(
        id=u["id"], email=u["email"], name_ar=u["name_ar"], name_en=u["name_en"],
        role=u["role"], is_active=u["is_active"], company_id=u["company_id"],
        last_login=u.get("last_login"),
    )
