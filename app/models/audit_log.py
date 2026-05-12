from sqlalchemy import String, ForeignKey, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import BaseModel


class AuditLog(BaseModel):
    __tablename__ = "audit_log"

    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    user_name: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    before_data: Mapped[str | None] = mapped_column(Text)   # JSON
    after_data: Mapped[str | None] = mapped_column(Text)    # JSON
    ip_address: Mapped[str | None] = mapped_column(String(50))
    device_info: Mapped[str | None] = mapped_column(String(300))
    is_override: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("companies.id"), index=True)

    user: Mapped["User | None"] = relationship("User", foreign_keys=[user_id])
