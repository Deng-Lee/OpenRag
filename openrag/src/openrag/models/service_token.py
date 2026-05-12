"""Service token models for machine-to-machine API access"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.user import User


class ServiceToken(Base, TimestampMixin):
    """长期凭证；可授权访问多个工作区，绑定关系由 ServiceTokenWorkspace 管理。"""

    __tablename__ = "service_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    secret: Mapped[str] = mapped_column(
        String(256),
        unique=True,
        nullable=False,
        index=True,
        comment="Full service secret string (plaintext at rest per product spec)",
    )
    name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Optional human-readable label",
    )
    created_by_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="User who created the token",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the token was revoked (UTC)",
    )

    @validates("secret", "name")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    created_by: Mapped["User"] = relationship(
        "User",
        back_populates="created_service_tokens",
        foreign_keys=[created_by_user_id],
    )

    __table_args__ = (
        Index("idx_service_tokens_created_by", "created_by_user_id"),
    )

    def __repr__(self) -> str:
        return f"<ServiceToken(id={self.id}, name={self.name!r})>"