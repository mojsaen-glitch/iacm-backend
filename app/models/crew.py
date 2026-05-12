from sqlalchemy import String, Boolean, Float, ForeignKey, Date, DateTime, Text, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime
from app.db.base import BaseModel


class Crew(BaseModel):
    __tablename__ = "crew"

    # Identity
    employee_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    full_name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(100))
    company_id: Mapped[str] = mapped_column(String(36), ForeignKey("companies.id"), nullable=False, index=True)

    # Professional
    base: Mapped[str] = mapped_column(String(10), nullable=False)  # IATA airport code
    rank: Mapped[str] = mapped_column(String(50), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(20), default="short_haul")  # short_haul, long_haul, both
    contract_type: Mapped[str] = mapped_column(String(20), default="full_time")
    aircraft_qualifications: Mapped[str | None] = mapped_column(Text)  # JSON array
    languages: Mapped[str | None] = mapped_column(Text)  # JSON array

    # Status
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    block_reason: Mapped[str | None] = mapped_column(Text)
    blocked_by: Mapped[str | None] = mapped_column(String(36))
    blocked_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Personal
    nationality: Mapped[str | None] = mapped_column(String(100))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(10))
    join_date: Mapped[date | None] = mapped_column(Date)
    photo_path: Mapped[str | None] = mapped_column(String(500))

    # Contact
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))

    # Flight Hours (computed & stored for performance)
    monthly_flight_hours: Mapped[float] = mapped_column(Float, default=0.0)
    yearly_flight_hours: Mapped[float] = mapped_column(Float, default=0.0)
    total_flight_hours: Mapped[float] = mapped_column(Float, default=0.0)
    last_28day_hours: Mapped[float] = mapped_column(Float, default=0.0)
    last_flight_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_landing_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rest_hours_due: Mapped[float] = mapped_column(Float, default=0.0)
    available_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_monthly_hours: Mapped[float] = mapped_column(Float, default=100.0)

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="crew")
    documents: Mapped[list["CrewDocument"]] = relationship("CrewDocument", back_populates="crew", lazy="select")
    training_records: Mapped[list["TrainingRecord"]] = relationship("TrainingRecord", back_populates="crew", lazy="select")
    assignments: Mapped[list["Assignment"]] = relationship("Assignment", back_populates="crew", lazy="select")
    leave_requests: Mapped[list["LeaveRequest"]] = relationship("LeaveRequest", back_populates="crew", lazy="select")
