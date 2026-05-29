"""Create trace and retrieval evaluation tables.

Revision ID: 20260527_0001
Revises:
Create Date: 2026-05-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260527_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "document_parse_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("source_doc_hash", sa.String(length=128), nullable=False),
        sa.Column("parser_name", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_text_hash", sa.String(length=128), nullable=True),
        sa.Column("canonical_json_bucket", sa.String(length=128), nullable=False),
        sa.Column("canonical_json_object_key", sa.String(length=1024), nullable=False),
        sa.Column("canonical_json_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("canonical_md_bucket", sa.String(length=128), nullable=False),
        sa.Column("canonical_md_object_key", sa.String(length=1024), nullable=False),
        sa.Column("canonical_md_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("block_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("block_type_counts", _json_type(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", name="uq_document_parse_artifacts_artifact_id"),
    )
    op.create_index("ix_document_parse_artifacts_artifact_id", "document_parse_artifacts", ["artifact_id"])
    op.create_index("ix_document_parse_artifacts_file_id", "document_parse_artifacts", ["file_id"])
    op.create_index("ix_document_parse_artifacts_workspace_id", "document_parse_artifacts", ["workspace_id"])
    op.create_index(
        "idx_document_parse_artifacts_workspace_created",
        "document_parse_artifacts",
        ["workspace_id", "created_at"],
    )
    op.create_index("idx_document_parse_artifacts_text_hash", "document_parse_artifacts", ["canonical_text_hash"])
    op.create_index(
        "uq_document_parse_artifact_source_parser",
        "document_parse_artifacts",
        ["file_id", "source_doc_hash", "parser_name", "parser_version"],
        unique=True,
    )

    op.create_table(
        "eval_datasets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metadata", _json_type(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_eval_datasets_workspace_name", "eval_datasets", ["workspace_id", "name"], unique=True)
    op.create_index("idx_eval_datasets_workspace_created", "eval_datasets", ["workspace_id", "created_at"])

    op.create_table(
        "eval_queries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("query_hash", sa.String(length=128), nullable=False),
        sa.Column("query_type", sa.String(length=64), nullable=True),
        sa.Column("expected_answer", sa.Text(), nullable=True),
        sa.Column("metadata", _json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["eval_datasets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_eval_queries_dataset_query_hash", "eval_queries", ["dataset_id", "query_hash"], unique=True)
    op.create_index("idx_eval_queries_dataset", "eval_queries", ["dataset_id"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("search_config_snapshot", _json_type(), nullable=True),
        sa.Column("code_version", sa.String(length=128), nullable=True),
        sa.Column("index_version", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("metadata", _json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dataset_id"], ["eval_datasets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_runs_dataset_created", "eval_runs", ["dataset_id", "created_at"])
    op.create_index("idx_eval_runs_status_created", "eval_runs", ["status", "created_at"])

    op.create_table(
        "trace_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("trace_type", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("eval_run_id", sa.Integer(), nullable=True),
        sa.Column("eval_query_id", sa.Integer(), nullable=True),
        sa.Column("query_hash", sa.String(length=128), nullable=True),
        sa.Column("query_preview", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_stage", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sampling_reason", sa.String(length=128), nullable=True),
        sa.Column("search_config_snapshot", _json_type(), nullable=True),
        sa.Column("otel_trace_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["eval_query_id"], ["eval_queries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["eval_run_id"], ["eval_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", name="uq_trace_runs_trace_id"),
    )
    op.create_index("idx_trace_runs_trace_id", "trace_runs", ["trace_id"], unique=True)
    op.create_index("idx_trace_runs_type_created", "trace_runs", ["trace_type", "created_at"])
    op.create_index("idx_trace_runs_workspace_created", "trace_runs", ["workspace_id", "created_at"])
    op.create_index("idx_trace_runs_file_created", "trace_runs", ["file_id", "created_at"])
    op.create_index("idx_trace_runs_task_id", "trace_runs", ["task_id"])
    op.create_index("idx_trace_runs_eval_query", "trace_runs", ["eval_run_id", "eval_query_id"])
    op.create_index("idx_trace_runs_query_hash_created", "trace_runs", ["query_hash", "created_at"])

    op.create_table(
        "trace_spans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("parent_span_id", sa.String(length=64), nullable=True),
        sa.Column("stage", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("input_summary", _json_type(), nullable=True),
        sa.Column("output_summary", _json_type(), nullable=True),
        sa.Column("metrics", _json_type(), nullable=True),
        sa.Column("artifact_refs", _json_type(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("otel_span_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("span_id", name="uq_trace_spans_span_id"),
    )
    op.create_index("idx_trace_spans_trace_started", "trace_spans", ["trace_id", "started_at"])
    op.create_index("idx_trace_spans_trace_stage", "trace_spans", ["trace_id", "stage"])
    op.create_index("idx_trace_spans_stage_created", "trace_spans", ["stage", "created_at"])

    op.create_table(
        "trace_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("stage", sa.String(length=128), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=True),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("score_parts", _json_type(), nullable=True),
        sa.Column("metadata", _json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_trace_snapshots_trace_stage_rank", "trace_snapshots", ["trace_id", "stage", "rank"])
    op.create_index("idx_trace_snapshots_chunk_created", "trace_snapshots", ["chunk_id", "created_at"])
    op.create_index("idx_trace_snapshots_file_created", "trace_snapshots", ["file_id", "created_at"])

    op.create_table(
        "trace_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=False),
        sa.Column("bucket", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("metadata", _json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", name="uq_trace_artifacts_artifact_id"),
    )
    op.create_index("idx_trace_artifacts_trace_created", "trace_artifacts", ["trace_id", "created_at"])
    op.create_index("idx_trace_artifacts_expires", "trace_artifacts", ["expires_at"])

    op.create_table(
        "eval_judgments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("eval_query_id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=True),
        sa.Column("file_id", sa.Integer(), nullable=True),
        sa.Column("relevance_grade", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("judge_user_id", sa.Integer(), nullable=True),
        sa.Column("judge_model", sa.String(length=128), nullable=True),
        sa.Column("judge_reason_ref", sa.String(length=1024), nullable=True),
        sa.Column("metadata", _json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("relevance_grade >= 0 AND relevance_grade <= 3", name="ck_eval_judgments_relevance_grade"),
        sa.CheckConstraint(
            "source IN ('gold_manual', 'business_import', 'llm_assisted')",
            name="ck_eval_judgments_source",
        ),
        sa.ForeignKeyConstraint(["dataset_id"], ["eval_datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["eval_query_id"], ["eval_queries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["judge_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_judgments_query_source", "eval_judgments", ["eval_query_id", "source"])
    op.create_index("idx_eval_judgments_chunk", "eval_judgments", ["chunk_id"])
    op.create_index("idx_eval_judgments_file", "eval_judgments", ["file_id"])

    op.create_table(
        "eval_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("eval_run_id", sa.Integer(), nullable=False),
        sa.Column("eval_query_id", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("metric_scope", sa.String(length=64), nullable=False),
        sa.Column("source_scope", sa.String(length=32), nullable=False),
        sa.Column("metrics", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["eval_query_id"], ["eval_queries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["eval_run_id"], ["eval_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_results_run_scope", "eval_results", ["eval_run_id", "metric_scope"])
    op.create_index("idx_eval_results_query", "eval_results", ["eval_query_id"])
    op.create_index("idx_eval_results_trace", "eval_results", ["trace_id"])


def downgrade() -> None:
    op.drop_index("idx_eval_results_trace", table_name="eval_results")
    op.drop_index("idx_eval_results_query", table_name="eval_results")
    op.drop_index("idx_eval_results_run_scope", table_name="eval_results")
    op.drop_table("eval_results")

    op.drop_index("idx_eval_judgments_file", table_name="eval_judgments")
    op.drop_index("idx_eval_judgments_chunk", table_name="eval_judgments")
    op.drop_index("idx_eval_judgments_query_source", table_name="eval_judgments")
    op.drop_table("eval_judgments")

    op.drop_index("idx_trace_artifacts_expires", table_name="trace_artifacts")
    op.drop_index("idx_trace_artifacts_trace_created", table_name="trace_artifacts")
    op.drop_table("trace_artifacts")

    op.drop_index("idx_trace_snapshots_file_created", table_name="trace_snapshots")
    op.drop_index("idx_trace_snapshots_chunk_created", table_name="trace_snapshots")
    op.drop_index("idx_trace_snapshots_trace_stage_rank", table_name="trace_snapshots")
    op.drop_table("trace_snapshots")

    op.drop_index("idx_trace_spans_stage_created", table_name="trace_spans")
    op.drop_index("idx_trace_spans_trace_stage", table_name="trace_spans")
    op.drop_index("idx_trace_spans_trace_started", table_name="trace_spans")
    op.drop_table("trace_spans")

    op.drop_index("idx_trace_runs_query_hash_created", table_name="trace_runs")
    op.drop_index("idx_trace_runs_eval_query", table_name="trace_runs")
    op.drop_index("idx_trace_runs_task_id", table_name="trace_runs")
    op.drop_index("idx_trace_runs_file_created", table_name="trace_runs")
    op.drop_index("idx_trace_runs_workspace_created", table_name="trace_runs")
    op.drop_index("idx_trace_runs_type_created", table_name="trace_runs")
    op.drop_index("idx_trace_runs_trace_id", table_name="trace_runs")
    op.drop_table("trace_runs")

    op.drop_index("idx_eval_runs_status_created", table_name="eval_runs")
    op.drop_index("idx_eval_runs_dataset_created", table_name="eval_runs")
    op.drop_table("eval_runs")

    op.drop_index("idx_eval_queries_dataset", table_name="eval_queries")
    op.drop_index("uq_eval_queries_dataset_query_hash", table_name="eval_queries")
    op.drop_table("eval_queries")

    op.drop_index("idx_eval_datasets_workspace_created", table_name="eval_datasets")
    op.drop_index("uq_eval_datasets_workspace_name", table_name="eval_datasets")
    op.drop_table("eval_datasets")

    op.drop_index("uq_document_parse_artifact_source_parser", table_name="document_parse_artifacts")
    op.drop_index("idx_document_parse_artifacts_text_hash", table_name="document_parse_artifacts")
    op.drop_index("idx_document_parse_artifacts_workspace_created", table_name="document_parse_artifacts")
    op.drop_index("ix_document_parse_artifacts_workspace_id", table_name="document_parse_artifacts")
    op.drop_index("ix_document_parse_artifacts_file_id", table_name="document_parse_artifacts")
    op.drop_index("ix_document_parse_artifacts_artifact_id", table_name="document_parse_artifacts")
    op.drop_table("document_parse_artifacts")
