"""Audit log model for tracking operations"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.user import User


class AuditLog(Base):
    """Audit log model for tracking operations"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False, comment="create, update, delete, read, share, etc.")
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="file, user, team, permission, share_link")
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, comment="JSON or text details")
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True, comment="IPv4 or IPv6")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    @validates("action", "resource_type", "details", "ip_address")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="audit_logs")

    # Indexes
    __table_args__ = (
        Index("idx_audit_user_id", "user_id"),
        Index("idx_audit_action", "action"),
        Index("idx_audit_resource", "resource_type", "resource_id"),
        Index("idx_audit_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, user_id={self.user_id}, action='{self.action}', resource_type='{self.resource_type}', resource_id={self.resource_id})>"
