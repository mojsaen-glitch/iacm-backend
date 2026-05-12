from pydantic import BaseModel, ConfigDict, EmailStr
from datetime import date, datetime
from typing import Optional, List
from app.schemas.common import BaseResponse


class CrewBase(BaseModel):
    employee_id: str
    full_name_ar: str
    full_name_en: str
    nickname: Optional[str] = None
    base: str
    rank: str
    operation_type: str = "short_haul"
    contract_type: str = "full_time"
    aircraft_qualifications: Optional[str] = None
    languages: Optional[str] = None
    nationality: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    join_date: Optional[date] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    max_monthly_hours: float = 100.0


class CrewCreate(CrewBase):
    company_id: str


class CrewUpdate(BaseModel):
    full_name_ar: Optional[str] = None
    full_name_en: Optional[str] = None
    nickname: Optional[str] = None
    base: Optional[str] = None
    rank: Optional[str] = None
    operation_type: Optional[str] = None
    contract_type: Optional[str] = None
    aircraft_qualifications: Optional[str] = None
    languages: Optional[str] = None
    nationality: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    max_monthly_hours: Optional[float] = None


class CrewResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    employee_id: str
    full_name_ar: str
    full_name_en: str
    nickname: Optional[str] = None
    company_id: str
    base: str
    rank: str
    operation_type: str
    contract_type: str
    aircraft_qualifications: Optional[str] = None
    languages: Optional[str] = None
    status: str
    block_reason: Optional[str] = None
    nationality: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    join_date: Optional[date] = None
    photo_path: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    monthly_flight_hours: float
    yearly_flight_hours: float
    total_flight_hours: float
    last_28day_hours: float
    last_flight_date: Optional[datetime] = None
    available_from: Optional[datetime] = None
    max_monthly_hours: float


class CrewBlockRequest(BaseModel):
    reason: str


class FTLStatus(BaseModel):
    crew_id: str
    monthly_hours: float
    last_28day_hours: float
    yearly_hours: float
    rest_hours_due: float
    available_from: Optional[datetime] = None
    is_available: bool
    violations: List[str] = []
