"""Long-lived parse artifact references for processed documents."""

from typing import Any, Dict, Optional

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes, strip_pg_nul_in_json


class DocumentParseArtifact(Base, TimestampMixin):
    """Reference to canonical parse artifacts stored outside PostgreSQL."""

    __tablename__ = "document_parse_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="Stable parse artifact identifier",
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Source file ID",
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Workspace ID",
    )
    source_doc_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Hash of the source document bytes used for parsing",
    )
    parser_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Parser implementation name",
    )
    parser_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Parser implementation version",
    )
    canonical_text_hash: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="Hash of the canonical readable text",
    )
    canonical_json_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_json_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    canonical_json_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    canonical_md_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_md_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    canonical_md_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    block_type_counts: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    @validates(
        "artifact_id",
        "source_doc_hash",
        "parser_name",
        "parser_version",
        "canonical_text_hash",
        "canonical_json_bucket",
        "canonical_json_object_key",
        "canonical_md_bucket",
        "canonical_md_object_key",
        "status",
        "error_message",
    )
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("block_type_counts")
    def _strip_nul_in_json_fields(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index(
            "uq_document_parse_artifact_source_parser",
            "file_id",
            "source_doc_hash",
            "parser_name",
            "parser_version",
            unique=True,
        ),
        Index("idx_document_parse_artifacts_workspace_created", "workspace_id", "created_at"),
        Index("idx_document_parse_artifacts_text_hash", "canonical_text_hash"),
    )
