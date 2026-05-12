"""Service token -> workspace binding model (multi-workspace authorization)."""

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref

from openrag.models.base import Base, TimestampMixin


class ServiceTokenWorkspace(Base, TimestampMixin):
    __tablename__ = "service_token_workspaces"
    __table_args__ = (
        UniqueConstraint("token_id", "workspace_id", name="uq_stw_token_workspace"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        ForeignKey("service_tokens.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="read or write",
    )

    token: Mapped["ServiceToken"] = relationship(backref=backref("workspace_bindings", passive_deletes=True))
    workspace: Mapped["Workspace"] = relationship()

    def __repr__(self) -> str:
        return f"<ServiceTokenWorkspace(token_id={self.token_id}, workspace_id={self.workspace_id}, permission={self.permission})>"