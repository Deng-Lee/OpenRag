"""Workspace models for multi-tenant isolation"""

from datetime import datetime
from typing import TYPE_CHECKING, List

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.user import User


class Workspace(Base, TimestampMixin):
    """Workspace model for multi-tenant isolation"""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
        comment="Workspace name (globally unique)",
    )
    slug: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
        index=True,
        comment="URL-friendly workspace identifier"
    )
    description: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Workspace description"
    )
    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Workspace owner user ID"
    )
    max_concurrent_tasks: Mapped[int] = mapped_column(
        Integer,
        default=10,
        nullable=False,
        comment="Maximum concurrent processing tasks"
    )
    max_storage_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=10737418240,  # 10GB
        nullable=False,
        comment="Maximum storage in bytes"
    )
    priority_strategy: Mapped[str] = mapped_column(
        String(32),
        default="file_size",
        nullable=False,
        comment="Task priority strategy"
    )

    @validates("name", "slug", "description", "priority_strategy")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    owner: Mapped["User"] = relationship(
        "User",
        back_populates="owned_workspaces",
        foreign_keys=[owner_id]
    )
    members: Mapped[List["WorkspaceMember"]] = relationship(
        "WorkspaceMember",
        back_populates="workspace",
        cascade="all, delete-orphan"
    )

    # Indexes
    __table_args__ = (
        Index("idx_workspace_slug", "slug"),
        Index("idx_workspace_owner", "owner_id"),
    )

    def __repr__(self) -> str:
        return f"<Workspace(id={self.id}, name='{self.name}', slug='{self.slug}')>"


class WorkspaceMember(Base):
    """Workspace member association model"""

    __tablename__ = "workspace_members"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        comment="Workspace ID"
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="User ID"
    )
    role: Mapped[str] = mapped_column(
        String(32),
        default="read",
        nullable=False,
        comment="Member role (read, write). read=只读, write=读写(增删改查)"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="Timestamp when user joined workspace"
    )

    @validates("role")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    workspace: Mapped["Workspace"] = relationship(
        "Workspace",
        back_populates="members"
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="workspace_memberships"
    )

    # Constraints and Indexes
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_user"),
        Index("idx_workspace_member", "workspace_id", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<WorkspaceMember(id={self.id}, workspace_id={self.workspace_id}, user_id={self.user_id}, role='{self.role}')>"
