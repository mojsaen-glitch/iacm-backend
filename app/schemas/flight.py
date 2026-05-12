from pydantic import BaseModel, ConfigDict, model_validator
from datetime import datetime
from typing import Optional
from app.schemas.common import BaseResponse


class FlightBase(BaseModel):
    flight_number: str
    origin_code: str
    destination_code: str
    departure_time: datetime
    arrival_time: datetime
    crew_required: int = 4
    notes: Optional[str] = None
    gate: Optional[str] = None

    @model_validator(mode="after")
    def compute_duration(self):
        diff = (self.arrival_time - self.departure_time).total_seconds() / 3600
        if diff <= 0:
            raise ValueError("arrival_time must be after departure_time")
        return self


class FlightCreate(FlightBase):
    company_id: str
    aircraft_id: Optional[str] = None


class FlightUpdate(BaseModel):
    flight_number: Optional[str] = None
    aircraft_id: Optional[str] = None
    departure_time: Optional[datetime] = None
    arrival_time: Optional[datetime] = None
    crew_required: Optional[int] = None
    status: Optional[str] = None
    publish_status: Optional[str] = None
    delay_minutes: Optional[int] = None
    delay_reason: Optional[str] = None
    gate: Optional[str] = None
    notes: Optional[str] = None


class FlightResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    flight_number: str
    company_id: str
    aircraft_id: Optional[str] = None
    origin_code: str
    destination_code: str
    departure_time: datetime
    arrival_time: datetime
    duration_hours: float
    crew_required: int
    status: str
    publish_status: str
    delay_minutes: int
    delay_reason: Optional[str] = None
    gate: Optional[str] = None
    notes: Optional[str] = None
    assigned_crew_count: int = 0
