from typing import Annotated
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import Client
from app.db.supabase_client import get_supabase
from app.core.security import decode_token
from app.core.exceptions import UnauthorizedError, ForbiddenError

security = HTTPBearer()

# Supabase client dependency
def get_sb() -> Client:
    return get_supabase()

SbClient = Annotated[Client, Depends(get_sb)]

# Backward compatibility alias
DbSession = SbClient

def get_supabase_dep():
    return Depends(get_sb)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    sb: SbClient,
) -> dict:
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise UnauthorizedError("Invalid or expired token")

    user_id = payload.get("sub")
    result = sb.table("users").select("*").eq("id", user_id).eq("is_active", True).execute()
    if not result.data:
        raise UnauthorizedError("User not found or inactive")

    return result.data[0]


CurrentUser = Annotated[dict, Depends(get_current_user)]


def require_roles(*roles: str):
    async def role_checker(current_user: CurrentUser) -> dict:
        if current_user["role"] not in roles and not current_user.get("is_superuser"):
            raise ForbiddenError(f"Role '{current_user['role']}' not allowed. Required: {list(roles)}")
        return current_user
    return Depends(role_checker)


AdminOnly = require_roles("super_admin", "admin")
OpsManager = require_roles("super_admin", "admin", "ops_manager")
SchedulerAccess = require_roles("super_admin", "admin", "ops_manager", "scheduler")
ComplianceAccess = require_roles("super_admin", "admin", "compliance_officer")
FlightOpsAccess = require_roles("super_admin", "admin", "ops_manager", "flight_ops", "scheduler")
