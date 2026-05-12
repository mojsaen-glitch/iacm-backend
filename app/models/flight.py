from sqlalchemy import String, Float, Integer, ForeignKey, DateTime, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from app.db.base import BaseModel


class Flight(BaseModel):
    __tablename__ = "flights"

    flight_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    aircraft_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("aircraft.id"))
    origin_code: Mapped[str] = mapped_column(String(4), nullable=False)
    destination_code: Mapped[str] = mapped_column(String(4), nullable=False)
    departure_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arrival_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_hours: Mapped[float] = mapped_column(Float, nullable=False)
    crew_required: Mapped[int] = mapped_column(Integer, default=4)

    # Status
    status: Mapped[str] = mapped_column(String(30), default="scheduled", index=True)
    # scheduled | boarding | in_air | landed | cancelled | delayed | draft
    publish_status: Mapped[str] = mapped_column(String(20), default="draft")
    # draft | published | archived

    # Operational
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    delay_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    gate: Mapped[str | None] = mapped_column(String(10))

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="flights")
    aircraft: Mapped["Aircraft | None"] = relationship("Aircraft", back_populates="flights")
    assignments: Mapped[list["Assignment"]] = relationship("Assignment", back_populates="flight", lazy="select")
