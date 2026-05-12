from sqlalchemy import String, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import BaseModel


class Setting(BaseModel):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("key", "company_id"),)

    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    company_id: Mapped[str] = mapped_column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(300))

    company: Mapped["Company"] = relationship("Company", back_populates="settings")
