"""
Flight Time Limitation (FTL) Service
Implements EASA OPS/CAR-OPS regulations for crew flight time limits.
"""
from datetime import datetime, timedelta, timezone
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from app.models.crew import Crew
from app.models.assignment import Assignment
from app.models.flight import Flight
from app.schemas.crew import FTLStatus
from app.core.config import settings


class FTLService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def check_crew_ftl(
        self,
        crew: Crew,
        proposed_flight: Flight,
        company_max_monthly: float = None,
        company_min_rest: float = None,
    ) -> FTLStatus:
        max_monthly = company_max_monthly or settings.MAX_MONTHLY_HOURS
        min_rest = company_min_rest or settings.MIN_REST_HOURS

        violations: List[str] = []

        # Check if crew is blocked
        if crew.status == "blocked":
            violations.append(f"Crew is blocked: {crew.block_reason or 'No reason given'}")

        # Check monthly hours limit
        projected_monthly = crew.monthly_flight_hours + proposed_flight.duration_hours
        if projected_monthly > max_monthly:
            violations.append(
                f"Monthly hours limit exceeded: {crew.monthly_flight_hours:.1f}h + "
                f"{proposed_flight.duration_hours:.1f}h = {projected_monthly:.1f}h > {max_monthly}h"
            )

        # Check 28-day rolling limit (EASA: 100h per 28 days)
        rolling_28 = crew.last_28day_hours + proposed_flight.duration_hours
        if rolling_28 > 100:
            violations.append(
                f"28-day rolling limit exceeded: {crew.last_28day_hours:.1f}h + "
                f"{proposed_flight.duration_hours:.1f}h = {rolling_28:.1f}h > 100h"
            )

        # Check minimum rest between flights
        if crew.last_landing_time:
            rest_available = (proposed_flight.departure_time - crew.last_landing_time).total_seconds() / 3600
            if rest_available < min_rest:
                violations.append(
                    f"Insufficient rest: {rest_available:.1f}h available < {min_rest}h required"
                )

        # Check available_from constraint
        if crew.available_from and proposed_flight.departure_time < crew.available_from:
            violations.append(
                f"Crew not available until {crew.available_from.isoformat()}"
            )

        is_available = len(violations) == 0

        return FTLStatus(
            crew_id=crew.id,
            monthly_hours=crew.monthly_flight_hours,
            last_28day_hours=crew.last_28day_hours,
            yearly_hours=crew.yearly_flight_hours,
            rest_hours_due=crew.rest_hours_due,
            available_from=crew.available_from,
            is_available=is_available,
            violations=violations,
        )

    async def recalculate_crew_hours(self, crew_id: str) -> Crew:
        """Recalculate flight hours from actual assignment records."""
        from sqlalchemy.orm import joinedload
        from datetime import date

        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        days_28_ago = now - timedelta(days=28)

        result = await self.db.execute(
            select(Assignment)
            .join(Flight, Assignment.flight_id == Flight.id)
            .where(
                and_(
                    Assignment.crew_id == crew_id,
                    Flight.status.in_(["landed", "in_air"]),
                )
            )
        )
        assignments = result.scalars().all()

        total = monthly = yearly = rolling_28 = 0.0
        last_flight = None
        last_landing = None

        for a in assignments:
            flight = await self.db.get(Flight, a.flight_id)
            if not flight:
                continue
            h = flight.duration_hours
            total += h
            if flight.departure_time >= month_start:
                monthly += h
            if flight.departure_time >= year_start:
                yearly += h
            if flight.departure_time >= days_28_ago:
                rolling_28 += h
            if last_flight is None or flight.departure_time > last_flight:
                last_flight = flight.departure_time
                last_landing = flight.arrival_time

        crew = await self.db.get(Crew, crew_id)
        if crew:
            crew.total_flight_hours = total
            crew.monthly_flight_hours = monthly
            crew.yearly_flight_hours = yearly
            crew.last_28day_hours = rolling_28
            crew.last_flight_date = last_flight
            crew.last_landing_time = last_landing
            if last_landing:
                rest_end = last_landing + timedelta(hours=settings.MIN_REST_HOURS)
                crew.available_from = rest_end if rest_end > now else None
                crew.rest_hours_due = max(0, (rest_end - now).total_seconds() / 3600)

        return crew
