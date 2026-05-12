from typing import Optional, List, Sequence
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
from app.models.crew import Crew
from app.repositories.base import BaseRepository


class CrewRepository(BaseRepository[Crew]):
    def __init__(self, db: AsyncSession):
        super().__init__(Crew, db)

    async def get_by_employee_id(self, employee_id: str) -> Optional[Crew]:
        result = await self.db.execute(
            select(Crew).where(Crew.employee_id == employee_id)
        )
        return result.scalar_one_or_none()

    async def get_by_company(
        self,
        company_id: str,
        skip: int = 0,
        limit: int = 20,
        status: Optional[str] = None,
        rank: Optional[str] = None,
        search: Optional[str] = None,
    ) -> tuple[Sequence[Crew], int]:
        query = select(Crew).where(Crew.company_id == company_id)

        if status:
            query = query.where(Crew.status == status)
        if rank:
            query = query.where(Crew.rank == rank)
        if search:
            term = f"%{search}%"
            query = query.where(
                or_(
                    Crew.full_name_ar.ilike(term),
                    Crew.full_name_en.ilike(term),
                    Crew.employee_id.ilike(term),
                )
            )

        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        result = await self.db.execute(query.offset(skip).limit(limit))
        return result.scalars().all(), total

    async def get_available_crew(
        self,
        company_id: str,
        flight_departure: "datetime",
        flight_duration_hours: float,
        aircraft_type: Optional[str] = None,
    ) -> Sequence[Crew]:
        from datetime import datetime, timezone
        query = select(Crew).where(
            and_(
                Crew.company_id == company_id,
                Crew.status.in_(["active", "standby"]),
            )
        )
        if aircraft_type:
            query = query.where(Crew.aircraft_qualifications.ilike(f"%{aircraft_type}%"))

        result = await self.db.execute(query)
        return result.scalars().all()

    async def update_flight_hours(
        self,
        crew_id: str,
        additional_hours: float,
        flight_date: "datetime",
    ) -> Optional[Crew]:
        crew = await self.get(crew_id)
        if not crew:
            return None
        crew.total_flight_hours += additional_hours
        crew.monthly_flight_hours += additional_hours
        crew.yearly_flight_hours += additional_hours
        crew.last_28day_hours += additional_hours
        crew.last_flight_date = flight_date
        await self.db.flush()
        return crew
