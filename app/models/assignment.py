from sqlalchemy import String, ForeignKey, DateTime, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from app.db.base import BaseModel


class Assignment(BaseModel):
    __tablename__ = "assignments"

    flight_id: Mapped[str] = mapped_column(String(36), ForeignKey("flights.id"), nullable=False, index=True)
    crew_id: Mapped[str] = mapped_column(String(36), ForeignKey("crew.id"), nullable=False, index=True)
    assigned_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    assignment_type: Mapped[str] = mapped_column(String(30), default="regular")
    # regular | standby | relief | training

    # Acknowledgment
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Override tracking
    is_override: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(Text)
    override_approved_by: Mapped[str | None] = mapped_column(String(36))

    # Relationships
    flight: Mapped["Flight"] = relationship("Flight", back_populates="assignments")
    crew: Mapped["Crew"] = relationship("Crew", back_populates="assignments")
    assigned_by_user: Mapped["User"] = relationship("User", foreign_keys=[assigned_by])
