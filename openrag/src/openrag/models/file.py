"""File metadata model"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.chunking.document_type import DEFAULT_DOCUMENT_TYPE
from openrag.utils.pg_text import strip_pg_nul_bytes


class ProcessingStatus(enum.Enum):
    """Document processing status for hierarchy generation"""

    pending = "pending"
    parsing = "parsing"
    building_hierarchy = "building_hierarchy"
    embedding = "embedding"
    completed = "completed"
    failed = "failed"


if TYPE_CHECKING:
    from openrag.models.document_chunk import DocumentChunk
    from openrag.models.permission import FilePermission
    from openrag.models.share import ShareLink
    from openrag.models.user import User
    from openrag.models.workspace import Workspace


class File(Base, TimestampMixin):
    """File metadata model (not actual file storage)"""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    uri: Mapped[str] = mapped_column(
        String(767),
        nullable=False,
        comment="Logical path; unique per workspace",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="File name")
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, comment="Owner user ID"
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("files.id"),
        nullable=True,
        comment="Parent directory ID (self-referential)",
    )
    is_directory: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="Is directory"
    )
    size: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, comment="File size in bytes"
    )
    mime_type: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, comment="MIME type"
    )
    document_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_DOCUMENT_TYPE,
        comment="Document type for chunking pipeline",
    )
    tag: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="Per-workspace unique tag (single-file upload only)",
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False, comment="Workspace ID"
    )

    @validates(
        "uri",
        "name",
        "mime_type",
        "document_type",
        "tag",
        "l0_path",
        "l1_path",
        "l2_path",
        "l0_vector_id",
        "parser_type",
        "processing_status",
        "processing_error",
    )
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    # Relationships
    owner: Mapped["User"] = relationship(
        "User", back_populates="owned_files", foreign_keys=[owner_id]
    )
    workspace: Mapped["Workspace"] = relationship(
        "Workspace", foreign_keys=[workspace_id]
    )
    parent: Mapped[Optional["File"]] = relationship(
        "File",
        remote_side="File.id",
        back_populates="children",
        foreign_keys=[parent_id],
    )
    children: Mapped[List["File"]] = relationship(
        "File",
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys=[parent_id],
    )
    share_links: Mapped[List["ShareLink"]] = relationship(
        "ShareLink", back_populates="file", cascade="all, delete-orphan"
    )
    document_chunks: Mapped[List["DocumentChunk"]] = relationship(
        "DocumentChunk",
        back_populates="file",
        cascade="all, delete-orphan",
    )
    permissions: Mapped[List["FilePermission"]] = relationship(
        "FilePermission",
        back_populates="file",
        cascade="all, delete-orphan",
    )

    # Hierarchy fields (L0/L1/L2 support) — 存 MinIO path-style HTTP URL 或兼容旧本地路径
    l0_path: Mapped[Optional[str]] = mapped_column(
        String(2048), nullable=True, comment="L0 abstract URL (MinIO)"
    )
    l1_path: Mapped[Optional[str]] = mapped_column(
        String(2048), nullable=True, comment="L1 overview URL (MinIO)"
    )
    l2_path: Mapped[Optional[str]] = mapped_column(
        String(2048), nullable=True, comment="L2 chunks 目录 URL 前缀 (MinIO)"
    )
    l0_vector_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, comment="L0 vector ID in vector DB"
    )
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus),
        default=ProcessingStatus.pending,
        nullable=False,
        comment="Document processing status",
    )
    processing_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Last processing failure message (truncated when stored from worker)",
    )
    total_chunks: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="Total chunk count"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="Total token count"
    )
    parser_type: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="User selected parser type (auto, pdf, docx, etc.)"
    )
    child_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="Child count for directories"
    )
    last_aggregated_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True, comment="Last aggregation time for directories"
    )

    # Indexes
    __table_args__ = (
        UniqueConstraint("workspace_id", "uri", name="uq_files_workspace_uri"),
        UniqueConstraint("workspace_id", "tag", name="uq_files_workspace_tag"),
        Index("idx_file_owner_id", "owner_id"),
        Index("idx_file_parent_id", "parent_id"),
    )

    def __repr__(self) -> str:
        return f"<File(id={self.id}, name='{self.name}', uri='{self.uri}', is_directory={self.is_directory})>"
