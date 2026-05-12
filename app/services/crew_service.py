from typing import Optional
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from app.repositories.crew_repository import CrewRepository
from app.models.crew import Crew
from app.schemas.crew import CrewCreate, CrewUpdate, CrewResponse
from app.core.exceptions import NotFoundError, ConflictError
from app.services.audit_service import AuditService


class CrewService:
    def __init__(self, db: AsyncSession):
        self.repo = CrewRepository(db)
        self.db = db

    async def create_crew(self, data: CrewCreate, created_by: str) -> Crew:
        existing = await self.repo.get_by_employee_id(data.employee_id)
        if existing:
            raise ConflictError(f"Employee ID '{data.employee_id}' already exists")

        crew = Crew(**data.model_dump())
        crew = await self.repo.create(crew)

        await AuditService(self.db).log(
            user_id=created_by,
            action="crew.create",
            entity_type="crew",
            entity_id=crew.id,
            after_data=data.model_dump_json(),
            company_id=data.company_id,
        )
        return crew

    async def update_crew(self, crew_id: str, data: CrewUpdate, updated_by: str) -> Crew:
        crew = await self.repo.get(crew_id)
        if not crew:
            raise NotFoundError("Crew member", crew_id)

        before = crew.to_dict()
        crew = await self.repo.update(crew, data.model_dump(exclude_none=True))

        await AuditService(self.db).log(
            user_id=updated_by,
            action="crew.update",
            entity_type="crew",
            entity_id=crew_id,
            before_data=str(before),
            after_data=data.model_dump_json(exclude_none=True),
            company_id=crew.company_id,
        )
        return crew

    async def block_crew(self, crew_id: str, reason: str, blocked_by: str) -> Crew:
        crew = await self.repo.get(crew_id)
        if not crew:
            raise NotFoundError("Crew member", crew_id)

        crew.status = "blocked"
        crew.block_reason = reason
        crew.blocked_by = blocked_by
        crew.blocked_on = datetime.now(timezone.utc)
        crew = await self.repo.save(crew)

        await AuditService(self.db).log(
            user_id=blocked_by,
            action="crew.block",
            entity_type="crew",
            entity_id=crew_id,
            after_data=f'{{"reason": "{reason}"}}',
            company_id=crew.company_id,
        )
        return crew

    async def unblock_crew(self, crew_id: str, unblocked_by: str) -> Crew:
        crew = await self.repo.get(crew_id)
        if not crew:
            raise NotFoundError("Crew member", crew_id)

        crew.status = "active"
        crew.block_reason = None
        crew.blocked_by = None
        crew.blocked_on = None
        crew = await self.repo.save(crew)

        await AuditService(self.db).log(
            user_id=unblocked_by,
            action="crew.unblock",
            entity_type="crew",
            entity_id=crew_id,
            company_id=crew.company_id,
        )
        return crew

    async def get_crew(self, crew_id: str) -> Crew:
        crew = await self.repo.get(crew_id)
        if not crew:
            raise NotFoundError("Crew member", crew_id)
        return crew

    async def list_crew(
        self,
        company_id: str,
        skip: int = 0,
        limit: int = 20,
        status: Optional[str] = None,
        rank: Optional[str] = None,
        search: Optional[str] = None,
    ):
        return await self.repo.get_by_company(company_id, skip, limit, status, rank, search)

    async def upload_photo(self, crew_id: str, photo_path: str, updated_by: str) -> Crew:
        crew = await self.repo.get(crew_id)
        if not crew:
            raise NotFoundError("Crew member", crew_id)
        crew.photo_path = photo_path
        return await self.repo.save(crew)
