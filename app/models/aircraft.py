from sqlalchemy import String, Integer, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import BaseModel


class Aircraft(BaseModel):
    __tablename__ = "aircraft"

    company_id: Mapped[str] = mapped_column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    aircraft_type: Mapped[str] = mapped_column(String(50), nullable=False)
    registration: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    manufacturer: Mapped[str | None] = mapped_column(String(100))
    min_crew: Mapped[int] = mapped_column(Integer, default=2)
    max_crew: Mapped[int] = mapped_column(Integer, default=10)
    capacity: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    company: Mapped["Company"] = relationship("Company", back_populates="aircraft")
    flights: Mapped[list["Flight"]] = relationship("Flight", back_populates="aircraft", lazy="select")
