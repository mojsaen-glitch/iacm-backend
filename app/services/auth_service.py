from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from app.repositories.user_repository import UserRepository
from app.core.security import verify_password, create_access_token, create_refresh_token, decode_token
from app.core.exceptions import UnauthorizedError
from app.schemas.auth import LoginRequest, TokenResponse, CurrentUserResponse
from app.core.config import settings


class AuthService:
    def __init__(self, db: AsyncSession):
        self.user_repo = UserRepository(db)

    async def login(self, data: LoginRequest) -> TokenResponse:
        user = await self.user_repo.get_by_email(data.email)
        if not user or not verify_password(data.password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password")
        if not user.is_active:
            raise UnauthorizedError("Account is disabled")

        access_token = create_access_token(subject=user.id)
        refresh_token = create_refresh_token(subject=user.id)

        # Store refresh token & update last login
        user.refresh_token = refresh_token
        user.last_login = datetime.now(timezone.utc)
        await self.user_repo.save(user)

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        payload = decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid or expired refresh token")

        user = await self.user_repo.get_by_refresh_token(refresh_token)
        if not user or not user.is_active:
            raise UnauthorizedError("Invalid refresh token")

        new_access = create_access_token(subject=user.id)
        new_refresh = create_refresh_token(subject=user.id)
        user.refresh_token = new_refresh
        await self.user_repo.save(user)

        return TokenResponse(
            access_token=new_access,
            refresh_token=new_refresh,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def logout(self, user_id: str) -> None:
        user = await self.user_repo.get(user_id)
        if user:
            user.refresh_token = None
            await self.user_repo.save(user)

    async def get_current_user(self, user_id: str) -> CurrentUserResponse:
        user = await self.user_repo.get(user_id)
        if not user:
            raise UnauthorizedError("User not found")
        return CurrentUserResponse(
            id=user.id,
            email=user.email,
            name_ar=user.name_ar,
            name_en=user.name_en,
            role=user.role,
            company_id=user.company_id,
            crew_id=user.crew_id,
            is_active=user.is_active,
            avatar_path=user.avatar_path,
        )
