from sqlalchemy import String, Float, Boolean, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import BaseModel


class Route(BaseModel):
    __tablename__ = "routes"

    company_id: Mapped[str] = mapped_column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    origin_code: Mapped[str] = mapped_column(String(4), nullable=False)
    destination_code: Mapped[str] = mapped_column(String(4), nullable=False)
    flight_duration_hours: Mapped[float] = mapped_column(Float, nullable=False)
    is_international: Mapped[bool] = mapped_column(Boolean, default=False)
    required_rest_hours: Mapped[float] = mapped_column(Float, default=10.0)
    min_crew: Mapped[int] = mapped_column(Integer, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    company: Mapped["Company"] = relationship("Company", back_populates="routes")
