from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from app.models.assignment import Assignment
from app.models.flight import Flight
from app.models.crew import Crew
from app.repositories.crew_repository import CrewRepository
from app.repositories.flight_repository import FlightRepository
from app.schemas.assignment import AssignmentCreate, AssignmentResponse
from app.services.ftl_service import FTLService
from app.services.audit_service import AuditService
from app.core.exceptions import NotFoundError, FTLViolationError, CrewBlockedError, ConflictError


class AssignmentService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.crew_repo = CrewRepository(db)
        self.flight_repo = FlightRepository(db)
        self.ftl_service = FTLService(db)
        self.audit = AuditService(db)

    async def assign_crew(
        self,
        data: AssignmentCreate,
        assigned_by_id: str,
        company_id: str,
    ) -> Assignment:
        # Validate flight
        flight = await self.flight_repo.get(data.flight_id)
        if not flight or flight.company_id != company_id:
            raise NotFoundError("Flight", data.flight_id)

        # Validate crew
        crew = await self.crew_repo.get(data.crew_id)
        if not crew or crew.company_id != company_id:
            raise NotFoundError("Crew member", data.crew_id)

        # Check for duplicate assignment
        existing = await self.db.execute(
            select(Assignment).where(
                and_(
                    Assignment.flight_id == data.flight_id,
                    Assignment.crew_id == data.crew_id,
                )
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError(f"Crew member already assigned to flight {flight.flight_number}")

        # Check if crew is blocked (unless override)
        if crew.status == "blocked" and not data.is_override:
            raise CrewBlockedError(crew.full_name_en, crew.block_reason)

        # FTL Check (unless override)
        if not data.is_override:
            ftl_status = await self.ftl_service.check_crew_ftl(crew, flight)
            if not ftl_status.is_available:
                raise FTLViolationError("; ".join(ftl_status.violations))

        assignment = Assignment(
            flight_id=data.flight_id,
            crew_id=data.crew_id,
            assigned_by=assigned_by_id,
            assignment_type=data.assignment_type,
            is_override=data.is_override,
            override_reason=data.override_reason if data.is_override else None,
        )

        self.db.add(assignment)
        await self.db.flush()
        await self.db.refresh(assignment)

        await self.audit.log(
            user_id=assigned_by_id,
            action="assignment.create",
            entity_type="assignment",
            entity_id=assignment.id,
            after_data=data.model_dump_json(),
            is_override=data.is_override,
            override_reason=data.override_reason,
            company_id=company_id,
        )

        return assignment

    async def unassign_crew(
        self,
        assignment_id: str,
        unassigned_by: str,
        company_id: str,
    ) -> None:
        result = await self.db.execute(
            select(Assignment).where(Assignment.id == assignment_id)
        )
        assignment = result.scalar_one_or_none()
        if not assignment:
            raise NotFoundError("Assignment", assignment_id)

        await self.db.delete(assignment)
        await self.db.flush()

        await self.audit.log(
            user_id=unassigned_by,
            action="assignment.remove",
            entity_type="assignment",
            entity_id=assignment_id,
            company_id=company_id,
        )

    async def get_flight_crew(self, flight_id: str) -> list[Assignment]:
        result = await self.db.execute(
            select(Assignment).where(Assignment.flight_id == flight_id)
        )
        return list(result.scalars().all())

    async def acknowledge_assignment(self, assignment_id: str, crew_user_id: str) -> Assignment:
        from datetime import datetime, timezone
        result = await self.db.execute(
            select(Assignment).where(Assignment.id == assignment_id)
        )
        assignment = result.scalar_one_or_none()
        if not assignment:
            raise NotFoundError("Assignment", assignment_id)

        assignment.acknowledged = True
        assignment.acknowledged_at = datetime.now(timezone.utc)
        await self.db.flush()
        return assignment
