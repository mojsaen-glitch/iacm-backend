from sqlalchemy import String, ForeignKey, Date, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime
from app.db.base import BaseModel


class LeaveRequest(BaseModel):
    __tablename__ = "leave_requests"

    crew_id: Mapped[str] = mapped_column(String(36), ForeignKey("crew.id"), nullable=False, index=True)
    leave_type: Mapped[str] = mapped_column(String(50), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending | approved | rejected | cancelled
    approved_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    attachment_path: Mapped[str | None] = mapped_column(String(500))

    crew: Mapped["Crew"] = relationship("Crew", back_populates="leave_requests")
    approved_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[approved_by])
