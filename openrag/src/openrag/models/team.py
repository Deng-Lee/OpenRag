"""Team and TeamMember models"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.user import User
    from openrag.models.workspace import Workspace


class TeamRole(str, enum.Enum):
    """Team member role enum"""
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Team(Base, TimestampMixin):
    """Team model"""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, comment="Team name")
    description: Mapped[str] = mapped_column(String(512), nullable=True, comment="Team description")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True, comment="Team owner user ID")
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id"),
        nullable=True,
        comment="Workspace ID"
    )

    @validates("name", "description")
    def _strip_nul_strings_team(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    owner: Mapped["User"] = relationship("User", back_populates="owned_teams", foreign_keys=[owner_id])
    workspace: Mapped[Optional["Workspace"]] = relationship("Workspace", foreign_keys=[workspace_id])
    members: Mapped[List["TeamMember"]] = relationship(
        "TeamMember",
        back_populates="team",
        cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Team(id={self.id}, name='{self.name}', owner_id={self.owner_id})>"


class TeamMember(Base):
    """Team member model (many-to-many User-Team relationship)"""

    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, comment="Team ID")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, comment="User ID")
    role: Mapped[TeamRole] = mapped_column(
        Enum(TeamRole),
        nullable=False,
        default=TeamRole.MEMBER,
        comment="Member role"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="Join timestamp"
    )

    @validates("role")
    def _strip_nul_strings_member(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    team: Mapped["Team"] = relationship("Team", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="team_memberships")

    # Constraints
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_user"),
    )

    def __repr__(self) -> str:
        return f"<TeamMember(id={self.id}, team_id={self.team_id}, user_id={self.user_id}, role={self.role})>"
