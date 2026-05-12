"""FilePermission model (ACL)"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.file import File
    from openrag.models.team import Team
    from openrag.models.user import User
    from openrag.models.workspace import Workspace


class EntityType(str, enum.Enum):
    """Entity type enum for permissions"""
    USER = "user"
    TEAM = "team"


class Permission(str, enum.Enum):
    """Permission level enum"""
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class FilePermission(Base):
    """File permission model (ACL)"""

    __tablename__ = "file_permissions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"), nullable=False, comment="File ID")
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id"),
        nullable=True,
        comment="Workspace ID"
    )
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType),
        nullable=False,
        comment="Entity type (user or team)"
    )
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="User ID or Team ID")
    permission: Mapped[Permission] = mapped_column(
        Enum(Permission),
        nullable=False,
        comment="Permission level"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="Creation timestamp"
    )

    @validates("entity_type", "permission")
    def _strip_nul_enums(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    file: Mapped["File"] = relationship("File", back_populates="permissions")
    workspace: Mapped[Optional["Workspace"]] = relationship("Workspace", foreign_keys=[workspace_id])

    # Note: These relationships use viewonly=True because entity_id is polymorphic
    user: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[entity_id],
        primaryjoin="and_(FilePermission.entity_id == User.id, FilePermission.entity_type == 'user')",
        viewonly=True,
    )
    team: Mapped[Optional["Team"]] = relationship(
        "Team",
        foreign_keys=[entity_id],
        primaryjoin="and_(FilePermission.entity_id == Team.id, FilePermission.entity_type == 'team')",
        viewonly=True,
    )

    # Constraints and Indexes
    __table_args__ = (
        UniqueConstraint("file_id", "entity_type", "entity_id", name="uq_file_entity"),
        Index("idx_permission_file_id", "file_id"),
        Index("idx_permission_entity", "entity_type", "entity_id"),
    )

    def __repr__(self) -> str:
        return f"<FilePermission(id={self.id}, file_id={self.file_id}, entity_type={self.entity_type}, entity_id={self.entity_id}, permission={self.permission})>"
