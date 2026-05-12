from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.audit_log import AuditLog


class AuditService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def log(
        self,
        user_id: Optional[str],
        action: str,
        entity_type: str,
        entity_id: Optional[str] = None,
        before_data: Optional[str] = None,
        after_data: Optional[str] = None,
        ip_address: Optional[str] = None,
        device_info: Optional[str] = None,
        is_override: bool = False,
        override_reason: Optional[str] = None,
        company_id: Optional[str] = None,
        user_name: str = "System",
    ) -> AuditLog:
        log = AuditLog(
            user_id=user_id,
            user_name=user_name,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_data=before_data,
            after_data=after_data,
            ip_address=ip_address,
            device_info=device_info,
            is_override=is_override,
            override_reason=override_reason,
            company_id=company_id,
        )
        self.db.add(log)
        await self.db.flush()
        return log
