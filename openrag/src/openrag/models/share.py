"""ShareLink model"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.file import File
    from openrag.models.user import User
    from openrag.models.workspace import Workspace


class ShareLink(Base):
    """Share link model for file sharing"""

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"), nullable=False, comment="File ID")
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id"),
        nullable=True,
        comment="Workspace ID"
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="Unique share token")
    password_hash: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Optional password hash"
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="Optional expiration timestamp"
    )
    max_access_count: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Optional maximum access count"
    )
    access_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default='0',
        nullable=False,
        comment="Current access count"
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, comment="Creator user ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="Creation timestamp"
    )

    @validates("token", "password_hash")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    file: Mapped["File"] = relationship("File", back_populates="share_links")
    workspace: Mapped[Optional["Workspace"]] = relationship("Workspace", foreign_keys=[workspace_id])
    creator: Mapped["User"] = relationship("User", back_populates="created_share_links", foreign_keys=[created_by])

    # Indexes
    __table_args__ = (
        Index("idx_share_token", "token"),
        Index("idx_share_file_id", "file_id"),
        Index("idx_share_created_by", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<ShareLink(id={self.id}, file_id={self.file_id}, token='{self.token}', access_count={self.access_count})>"
