"""User model"""

from typing import TYPE_CHECKING, List

from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.audit import AuditLog
    from openrag.models.file import File
    from openrag.models.service_token import ServiceToken
    from openrag.models.share import ShareLink
    from openrag.models.team import Team, TeamMember
    from openrag.models.workspace import Workspace, WorkspaceMember
    from openrag.models.role import Role


class User(Base, TimestampMixin):
    """User model"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, comment="Username"
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, comment="Email address"
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Password hash"
    )
    full_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Full name"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="Is user active"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="Is system administrator"
    )

    @validates("username", "email", "password_hash", "full_name")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    roles: Mapped[List["Role"]] = relationship(
        "Role",
        secondary="user_roles",
        viewonly=True,
    )
    owned_files: Mapped[List["File"]] = relationship(
        "File",
        back_populates="owner",
        foreign_keys="File.owner_id",
        cascade="all, delete-orphan",
    )
    owned_teams: Mapped[List["Team"]] = relationship(
        "Team",
        back_populates="owner",
        foreign_keys="Team.owner_id",
        cascade="all, delete-orphan",
    )
    team_memberships: Mapped[List["TeamMember"]] = relationship(
        "TeamMember", back_populates="user", cascade="all, delete-orphan"
    )
    created_share_links: Mapped[List["ShareLink"]] = relationship(
        "ShareLink",
        back_populates="creator",
        foreign_keys="ShareLink.created_by",
        cascade="all, delete-orphan",
    )
    audit_logs: Mapped[List["AuditLog"]] = relationship(
        "AuditLog", back_populates="user", cascade="all, delete-orphan"
    )
    owned_workspaces: Mapped[List["Workspace"]] = relationship(
        "Workspace",
        back_populates="owner",
        foreign_keys="Workspace.owner_id",
        cascade="all, delete-orphan",
    )
    workspace_memberships: Mapped[List["WorkspaceMember"]] = relationship(
        "WorkspaceMember", back_populates="user", cascade="all, delete-orphan"
    )
    created_service_tokens: Mapped[List["ServiceToken"]] = relationship(
        "ServiceToken",
        back_populates="created_by",
        foreign_keys="ServiceToken.created_by_user_id",
        cascade="all, delete-orphan",
    )

    # Indexes
    __table_args__ = (
        Index("idx_username", "username"),
        Index("idx_email", "email"),
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username='{self.username}', email='{self.email}')>"
