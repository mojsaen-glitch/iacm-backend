from sqlalchemy import String, ForeignKey, Date
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date
from app.db.base import BaseModel


class TrainingRecord(BaseModel):
    __tablename__ = "training"

    crew_id: Mapped[str] = mapped_column(String(36), ForeignKey("crew.id"), nullable=False, index=True)
    training_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aircraft_type: Mapped[str | None] = mapped_column(String(50))
    completion_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    trainer: Mapped[str | None] = mapped_column(String(200))
    training_center: Mapped[str | None] = mapped_column(String(200))
    certificate_path: Mapped[str | None] = mapped_column(String(500))
    score: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="valid")  # valid, expiring, expired

    crew: Mapped["Crew"] = relationship("Crew", back_populates="training_records")
