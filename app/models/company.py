from sqlalchemy import String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import BaseModel


class Company(BaseModel):
    __tablename__ = "companies"

    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    logo_path: Mapped[str | None] = mapped_column(String(500))
    primary_color: Mapped[str | None] = mapped_column(String(20))
    secondary_color: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(100))
    icao_code: Mapped[str | None] = mapped_column(String(4))
    iata_code: Mapped[str | None] = mapped_column(String(3))
    contact_email: Mapped[str | None] = mapped_column(String(200))
    contact_phone: Mapped[str | None] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    users: Mapped[list["User"]] = relationship("User", back_populates="company", lazy="select")
    crew: Mapped[list["Crew"]] = relationship("Crew", back_populates="company", lazy="select")
    aircraft: Mapped[list["Aircraft"]] = relationship("Aircraft", back_populates="company", lazy="select")
    routes: Mapped[list["Route"]] = relationship("Route", back_populates="company", lazy="select")
    flights: Mapped[list["Flight"]] = relationship("Flight", back_populates="company", lazy="select")
    settings: Mapped[list["Setting"]] = relationship("Setting", back_populates="company", lazy="select")
