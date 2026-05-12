from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from app.api.deps import SbClient, CurrentUser
from app.schemas.auth import LoginRequest, TokenResponse, RefreshTokenRequest, CurrentUserResponse
from app.core.security import verify_password, create_access_token, create_refresh_token, decode_token
from app.core.exceptions import UnauthorizedError
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


@router.get("/me", response_model=CurrentUserResponse)
async def get_me(current_user: CurrentUser):
    return CurrentUserResponse(
        id=current_user["id"],
        email=current_user["email"],
        name_ar=current_user["name_ar"],
        name_en=current_user["name_en"],
        role=current_user["role"],
        company_id=current_user["company_id"],
        crew_id=current_user.get("crew_id"),
        is_active=current_user["is_active"],
        avatar_path=current_user.get("avatar_path"),
    )
