"""Trace store and retrieval evaluation models."""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes, strip_pg_nul_in_json


EVAL_JUDGMENT_SOURCE_WEIGHTS = {
    "gold_manual": 1.0,
    "business_import": 0.7,
    "llm_assisted": 0.4,
}


class TraceRun(Base):
    """One traced OpenRag operation."""

    __tablename__ = "trace_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    trace_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    file_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    eval_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="SET NULL"), nullable=True
    )
    eval_query_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("eval_queries.id", ondelete="SET NULL"), nullable=True
    )
    query_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    query_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_stage: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sampling_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    search_config_snapshot: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    otel_trace_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    @validates(
        "trace_id",
        "trace_type",
        "task_id",
        "query_hash",
        "query_preview",
        "status",
        "error_stage",
        "error_message",
        "sampling_reason",
        "otel_trace_id",
    )
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("search_config_snapshot")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("idx_trace_runs_trace_id", "trace_id", unique=True),
        Index("idx_trace_runs_type_created", "trace_type", "created_at"),
        Index("idx_trace_runs_workspace_created", "workspace_id", "created_at"),
        Index("idx_trace_runs_file_created", "file_id", "created_at"),
        Index("idx_trace_runs_task_id", "task_id"),
        Index("idx_trace_runs_eval_query", "eval_run_id", "eval_query_id"),
        Index("idx_trace_runs_query_hash_created", "query_hash", "created_at"),
    )


class TraceSpan(Base):
    """One stage within a trace run."""

    __tablename__ = "trace_spans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    span_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_span_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stage: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    output_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    metrics: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    artifact_refs: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    otel_span_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    @validates("span_id", "trace_id", "parent_span_id", "stage", "status", "error_message", "otel_span_id")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("input_summary", "output_summary", "metrics", "artifact_refs")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("idx_trace_spans_trace_started", "trace_id", "started_at"),
        Index("idx_trace_spans_trace_stage", "trace_id", "stage"),
        Index("idx_trace_spans_stage_created", "stage", "created_at"),
    )


class TraceSnapshot(Base):
    """Compact top-k snapshot captured during retrieval stages."""

    __tablename__ = "trace_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stage: Mapped[str] = mapped_column(String(128), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    file_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_parts: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    @validates("trace_id", "span_id", "stage", "chunk_id")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("score_parts", "metadata_")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("idx_trace_snapshots_trace_stage_rank", "trace_id", "stage", "rank"),
        Index("idx_trace_snapshots_chunk_created", "chunk_id", "created_at"),
        Index("idx_trace_snapshots_file_created", "file_id", "created_at"),
    )


class TraceArtifact(Base):
    """Short-lived trace artifact reference."""

    __tablename__ = "trace_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(32), default="minio", nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    @validates(
        "artifact_id",
        "trace_id",
        "span_id",
        "artifact_type",
        "storage_backend",
        "bucket",
        "object_key",
        "content_type",
    )
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("metadata_")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("idx_trace_artifacts_trace_created", "trace_id", "created_at"),
        Index("idx_trace_artifacts_expires", "expires_at"),
    )


class EvalDataset(Base, TimestampMixin):
    """Retrieval evaluation dataset."""

    __tablename__ = "eval_datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    @validates("name", "description", "status")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("metadata_")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("uq_eval_datasets_workspace_name", "workspace_id", "name", unique=True),
        Index("idx_eval_datasets_workspace_created", "workspace_id", "created_at"),
    )


class EvalQuery(Base, TimestampMixin):
    """One query in an evaluation dataset."""

    __tablename__ = "eval_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("eval_datasets.id", ondelete="CASCADE"), nullable=False
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    query_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expected_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    @validates("query_text", "query_hash", "query_type", "expected_answer")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("metadata_")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("uq_eval_queries_dataset_query_hash", "dataset_id", "query_hash", unique=True),
        Index("idx_eval_queries_dataset", "dataset_id"),
    )


class EvalJudgment(Base, TimestampMixin):
    """Human, imported, or LLM-assisted relevance judgment."""

    __tablename__ = "eval_judgments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("eval_datasets.id", ondelete="CASCADE"), nullable=False
    )
    eval_query_id: Mapped[int] = mapped_column(
        ForeignKey("eval_queries.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    file_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    relevance_grade: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="gold_manual", nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    judge_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    judge_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    judge_reason_ref: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)

    def __init__(self, **kwargs: Any) -> None:
        source = kwargs.get("source", "gold_manual")
        if kwargs.get("weight") is None:
            kwargs["weight"] = EVAL_JUDGMENT_SOURCE_WEIGHTS.get(source)
        super().__init__(**kwargs)

    @validates("relevance_grade")
    def _validate_relevance_grade(self, _key: str, value: int) -> int:
        return value

    @validates("source")
    def _validate_source(self, _key: str, value: str) -> str:
        value = strip_pg_nul_bytes(value)
        if getattr(self, "weight", None) is None:
            self.weight = EVAL_JUDGMENT_SOURCE_WEIGHTS.get(value)
        return value

    @validates("chunk_id", "judge_model", "judge_reason_ref")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("metadata_")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        CheckConstraint("relevance_grade >= 0 AND relevance_grade <= 3", name="ck_eval_judgments_relevance_grade"),
        CheckConstraint(
            "source IN ('gold_manual', 'business_import', 'llm_assisted')",
            name="ck_eval_judgments_source",
        ),
        Index("idx_eval_judgments_query_source", "eval_query_id", "source"),
        Index("idx_eval_judgments_chunk", "chunk_id"),
        Index("idx_eval_judgments_file", "file_id"),
    )


class EvalRun(Base):
    """One batch evaluation run for a dataset and search configuration."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("eval_datasets.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    search_config_snapshot: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    code_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    index_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    metadata_: Mapped[Optional[Dict[str, Any]]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    @validates("name", "status", "code_version", "index_version")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("search_config_snapshot", "metadata_")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("idx_eval_runs_dataset_created", "dataset_id", "created_at"),
        Index("idx_eval_runs_status_created", "status", "created_at"),
    )


class EvalResult(Base):
    """Metric payload for an eval query or eval run summary."""

    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    eval_run_id: Mapped[int] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    eval_query_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("eval_queries.id", ondelete="SET NULL"), nullable=True
    )
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metric_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(32), default="weighted_all", nullable=False)
    metrics: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    @validates("trace_id", "metric_scope", "source_scope")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("metrics")
    def _strip_nul_json(self, _key: str, value: object) -> object:
        return strip_pg_nul_in_json(value)

    __table_args__ = (
        Index("idx_eval_results_run_scope", "eval_run_id", "metric_scope"),
        Index("idx_eval_results_query", "eval_query_id"),
        Index("idx_eval_results_trace", "trace_id"),
    )
