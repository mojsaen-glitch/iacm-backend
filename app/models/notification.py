from sqlalchemy import String, ForeignKey, Boolean, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from app.db.base import BaseModel


class Notification(BaseModel):
    __tablename__ = "notifications"

    title_ar: Mapped[str] = mapped_column(String(300), nullable=False)
    title_en: Mapped[str] = mapped_column(String(300), nullable=False)
    body_ar: Mapped[str | None] = mapped_column(Text)
    body_en: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    # flight_assignment | document_expiry | ftl_warning | system | leave_request | etc.

    target_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    company_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("companies.id"), index=True)
    related_flight_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("flights.id"))
    related_crew_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("crew.id"))

    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requires_acknowledge: Mapped[bool] = mapped_column(Boolean, default=False)
    is_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    target_user: Mapped["User | None"] = relationship("User", foreign_keys=[target_user_id])
