from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional
from app.schemas.common import BaseResponse


class AssignmentCreate(BaseModel):
    flight_id: str
    crew_id: str
    assignment_type: str = "regular"
    is_override: bool = False
    override_reason: Optional[str] = None


class AssignmentUpdate(BaseModel):
    assignment_type: Optional[str] = None
    acknowledged: Optional[bool] = None


class AssignmentResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    flight_id: str
    crew_id: str
    assigned_by: str
    assignment_type: str
    acknowledged: bool
    acknowledged_at: Optional[datetime] = None
    is_override: bool
    override_reason: Optional[str] = None

    # Populated via join
    crew_name_ar: Optional[str] = None
    crew_name_en: Optional[str] = None
    crew_rank: Optional[str] = None
    flight_number: Optional[str] = None
