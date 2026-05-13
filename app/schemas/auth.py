from pydantic import BaseModel, EmailStr
from typing import Optional


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    def validate_new_password(self) -> bool:
        return len(self.new_password) >= 8


class CurrentUserResponse(BaseModel):
    id: str
    email: str
    name_ar: str
    name_en: str
    role: str
    crew_department: Optional[str] = None  # cabin | cockpit | ground
    company_id: str
    crew_id: str | None = None
    is_active: bool
    avatar_path: str | None = None


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    name_ar: str
    name_en: str
    role: str  # admin | ops_manager | scheduler | crew_allocator
               # cabin_allocator | cockpit_allocator | ground_allocator
               # compliance_officer | crew
    crew_department: Optional[str] = None  # cabin | cockpit | ground (for allocators)
    company_id: Optional[str] = None  # if None, inherit from creator


class UserListItem(BaseModel):
    id: str
    email: str
    name_ar: str
    name_en: str
    role: str
    is_active: bool
    company_id: str
    last_login: Optional[str] = None
