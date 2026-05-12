from typing import Optional, Sequence
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from app.models.flight import Flight
from app.repositories.base import BaseRepository


class FlightRepository(BaseRepository[Flight]):
    def __init__(self, db: AsyncSession):
        super().__init__(Flight, db)

    async def get_by_company(
        self,
        company_id: str,
        skip: int = 0,
        limit: int = 20,
        status: Optional[str] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> tuple[Sequence[Flight], int]:
        query = select(Flight).where(Flight.company_id == company_id)

        if status:
            query = query.where(Flight.status == status)
        if from_date:
            query = query.where(Flight.departure_time >= from_date)
        if to_date:
            query = query.where(Flight.departure_time <= to_date)

        count_query = select(func.count()).select_from(query.subquery())
        total = (await self.db.execute(count_query)).scalar_one()

        result = await self.db.execute(
            query.order_by(Flight.departure_time.asc()).offset(skip).limit(limit)
        )
        return result.scalars().all(), total

    async def get_by_flight_number(self, flight_number: str, company_id: str) -> Optional[Flight]:
        result = await self.db.execute(
            select(Flight).where(
                and_(Flight.flight_number == flight_number, Flight.company_id == company_id)
            )
        )
        return result.scalar_one_or_none()
