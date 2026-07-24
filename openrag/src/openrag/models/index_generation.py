"""Authoritative metadata for versioned search index generations."""

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from openrag.models.base import Base, TimestampMixin


class IndexGenerationState(str, enum.Enum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    BUILDING = "building"
    RECONCILING = "reconciling"
    VALIDATING = "validating"
    READY = "ready"
    ACTIVATING = "activating"
    ACTIVE = "active"
    RETIRED = "retired"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


class IndexGenerationFileState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    RETRY = "retry"
    FAILED = "failed"
    DELETED = "deleted"
    STALE = "stale"


_GENERATION_STATES = ", ".join(f"'{state.value}'" for state in IndexGenerationState)
_FILE_STATES = ", ".join(f"'{state.value}'" for state in IndexGenerationFileState)


class IndexGeneration(Base, TimestampMixin):
    """Immutable index manifest plus mutable build and lifecycle state."""

    __tablename__ = "index_generations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_request_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, unique=True
    )
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="global")
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=IndexGenerationState.DRAFT.value
    )
    source_generation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("index_generations.id", ondelete="SET NULL"), nullable=True
    )
    embedding_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(256), nullable=False)
    embedding_revision: Mapped[str] = mapped_column(String(256), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_config_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    vector_normalization: Mapped[str] = mapped_column(String(32), nullable=False)
    distance_metric: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_policy_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    hierarchy_policy_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_collection_name: Mapped[str] = mapped_column(String(255), nullable=False)
    layer_collection_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    es_generation: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_watermark_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    build_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    build_lag_files: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    mirror_lag_files: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_mirrored_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )

    expected_file_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    expected_chunk_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    expected_layer_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    indexed_file_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    indexed_chunk_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    indexed_layer_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    failed_file_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    validation_report: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    validation_started_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    validation_completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    validation_error_code: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    quality_report: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON, nullable=True
    )
    quality_gate_passed: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    quality_validated_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    build_started_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    ready_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    retired_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    delete_after: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    backup_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    backup_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    deletion_plan: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    deleted_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint(
            "chunk_collection_name", name="uq_index_generation_chunk_collection"
        ),
        UniqueConstraint(
            "layer_collection_name", name="uq_index_generation_layer_collection"
        ),
        CheckConstraint(
            f"state IN ({_GENERATION_STATES})", name="ck_index_generation_state"
        ),
        CheckConstraint(
            "embedding_dimension > 0", name="ck_index_generation_dimension_positive"
        ),
        CheckConstraint(
            "schema_version > 0", name="ck_index_generation_schema_version_positive"
        ),
        CheckConstraint(
            "expected_file_count >= 0", name="ck_index_generation_expected_files"
        ),
        CheckConstraint(
            "expected_chunk_count >= 0", name="ck_index_generation_expected_chunks"
        ),
        CheckConstraint(
            "expected_layer_count >= 0", name="ck_index_generation_expected_layers"
        ),
        CheckConstraint(
            "indexed_file_count >= 0", name="ck_index_generation_indexed_files"
        ),
        CheckConstraint(
            "indexed_chunk_count >= 0", name="ck_index_generation_indexed_chunks"
        ),
        CheckConstraint(
            "indexed_layer_count >= 0", name="ck_index_generation_indexed_layers"
        ),
        CheckConstraint(
            "failed_file_count >= 0", name="ck_index_generation_failed_files"
        ),
        CheckConstraint(
            "build_lag_files >= 0", name="ck_index_generation_build_lag"
        ),
        CheckConstraint(
            "mirror_lag_files >= 0", name="ck_index_generation_mirror_lag"
        ),
        CheckConstraint("lock_version >= 0", name="ck_index_generation_lock_version"),
        Index(
            "uq_index_generation_active_scope",
            "scope",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index("idx_index_generation_scope_state", "scope", "state"),
    )


class IndexGenerationRoute(Base):
    """Atomic route binding Chunk and Layer resources for one scope."""

    __tablename__ = "index_generation_routes"

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    active_generation_id: Mapped[str] = mapped_column(
        ForeignKey("index_generations.id", ondelete="RESTRICT"), nullable=False
    )
    previous_generation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("index_generations.id", ondelete="SET NULL"), nullable=True
    )
    route_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    activated_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rollback_deadline: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True
    )
    write_barrier: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        CheckConstraint("route_version >= 0", name="ck_index_generation_route_version"),
    )


class IndexGenerationFile(Base):
    """Recoverable per-file build status for a generation."""

    __tablename__ = "index_generation_files"

    generation_id: Mapped[str] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=IndexGenerationFileState.PENDING.value
    )
    source_content_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    source_updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    expected_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    written_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    expected_layer_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    written_layer_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"state IN ({_FILE_STATES})", name="ck_index_generation_file_state"
        ),
        CheckConstraint(
            "expected_chunk_count >= 0", name="ck_index_generation_file_expected_chunks"
        ),
        CheckConstraint(
            "written_chunk_count >= 0", name="ck_index_generation_file_written_chunks"
        ),
        CheckConstraint(
            "expected_layer_count >= 0", name="ck_index_generation_file_expected_layers"
        ),
        CheckConstraint(
            "written_layer_count >= 0", name="ck_index_generation_file_written_layers"
        ),
        CheckConstraint(
            "retry_count >= 0", name="ck_index_generation_file_retry_count"
        ),
        Index(
            "idx_index_generation_file_state_retry",
            "generation_id",
            "state",
            "next_retry_at",
        ),
        Index("idx_index_generation_file_workspace", "workspace_id", "generation_id"),
    )
