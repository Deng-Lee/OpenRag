from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from typing import List, TYPE_CHECKING

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.user import User
    from openrag.models.workspace import Workspace


class Role(Base, TimestampMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Role display name"
    )
    role_code: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        comment="System unique code (en_US and underscores)",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    workspace_permissions: Mapped[List["RoleWorkspacePermission"]] = relationship(
        "RoleWorkspacePermission", back_populates="role", cascade="all, delete-orphan"
    )
    user_assignments: Mapped[List["UserRole"]] = relationship(
        "UserRole", back_populates="role", cascade="all, delete-orphan"
    )

    @validates("name", "role_code", "description")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)


class RoleWorkspacePermission(Base, TimestampMixin):
    __tablename__ = "role_workspace_permissions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    permission: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="read or write"
    )

    @validates("permission")
    def _strip_nul_strings_rwp(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    role: Mapped["Role"] = relationship("Role", back_populates="workspace_permissions")
    workspace: Mapped["Workspace"] = relationship("Workspace")

    __table_args__ = (
        UniqueConstraint("role_id", "workspace_id", name="uq_role_workspace"),
    )


class UserRole(Base, TimestampMixin):
    __tablename__ = "user_roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )

    role: Mapped["Role"] = relationship("Role", back_populates="user_assignments")
    user: Mapped["User"] = relationship(
        "User",
        foreign_keys=[user_id],
        viewonly=True,
    )

    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)
