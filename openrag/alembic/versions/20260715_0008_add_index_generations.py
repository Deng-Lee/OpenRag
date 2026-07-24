"""Add authoritative index generation registry.

Revision ID: 20260715_0008
Revises: 20260715_0007
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0008"
down_revision: Union[str, None] = "20260715_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


GENERATION_STATES = (
    "draft",
    "provisioning",
    "building",
    "reconciling",
    "validating",
    "ready",
    "activating",
    "active",
    "retired",
    "deleting",
    "deleted",
    "failed",
)
FILE_STATES = ("pending", "running", "success", "retry", "failed", "deleted", "stale")


def _in_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "index_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column(
            "source_generation_id",
            sa.String(36),
            sa.ForeignKey("index_generations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("embedding_provider", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(256), nullable=False),
        sa.Column("embedding_revision", sa.String(256), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("embedding_fingerprint", sa.String(64), nullable=False),
        sa.Column("embedding_config_ref", sa.String(256), nullable=False),
        sa.Column("vector_normalization", sa.String(32), nullable=False),
        sa.Column("distance_metric", sa.String(16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("chunk_policy_revision", sa.String(128), nullable=False),
        sa.Column("hierarchy_policy_revision", sa.String(128), nullable=False),
        sa.Column("chunk_collection_name", sa.String(255), nullable=False),
        sa.Column("layer_collection_name", sa.String(255), nullable=True),
        sa.Column("es_generation", sa.String(128), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("source_watermark_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "expected_file_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "expected_chunk_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "expected_layer_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "indexed_file_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "indexed_chunk_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "indexed_layer_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "failed_file_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("validation_report", sa.JSON(), nullable=True),
        sa.Column("quality_report", sa.JSON(), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("build_started_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("ready_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("activated_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("retired_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("delete_after", sa.TIMESTAMP(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"state IN ({_in_values(GENERATION_STATES)})",
            name="ck_index_generation_state",
        ),
        sa.CheckConstraint(
            "embedding_dimension > 0", name="ck_index_generation_dimension_positive"
        ),
        sa.CheckConstraint(
            "schema_version > 0", name="ck_index_generation_schema_version_positive"
        ),
        sa.CheckConstraint(
            "expected_file_count >= 0", name="ck_index_generation_expected_files"
        ),
        sa.CheckConstraint(
            "expected_chunk_count >= 0", name="ck_index_generation_expected_chunks"
        ),
        sa.CheckConstraint(
            "expected_layer_count >= 0", name="ck_index_generation_expected_layers"
        ),
        sa.CheckConstraint(
            "indexed_file_count >= 0", name="ck_index_generation_indexed_files"
        ),
        sa.CheckConstraint(
            "indexed_chunk_count >= 0", name="ck_index_generation_indexed_chunks"
        ),
        sa.CheckConstraint(
            "indexed_layer_count >= 0", name="ck_index_generation_indexed_layers"
        ),
        sa.CheckConstraint(
            "failed_file_count >= 0", name="ck_index_generation_failed_files"
        ),
        sa.CheckConstraint(
            "lock_version >= 0", name="ck_index_generation_lock_version"
        ),
        sa.UniqueConstraint(
            "chunk_collection_name", name="uq_index_generation_chunk_collection"
        ),
        sa.UniqueConstraint(
            "layer_collection_name", name="uq_index_generation_layer_collection"
        ),
    )
    op.create_index(
        "uq_index_generation_active_scope",
        "index_generations",
        ["scope"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "idx_index_generation_scope_state", "index_generations", ["scope", "state"]
    )

    op.create_table(
        "index_generation_routes",
        sa.Column("scope", sa.String(32), primary_key=True),
        sa.Column(
            "active_generation_id",
            sa.String(36),
            sa.ForeignKey("index_generations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "previous_generation_id",
            sa.String(36),
            sa.ForeignKey("index_generations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("route_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("activated_at", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "activated_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rollback_deadline", sa.TIMESTAMP(), nullable=True),
        sa.Column(
            "write_barrier",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "route_version >= 0", name="ck_index_generation_route_version"
        ),
    )

    op.create_table(
        "index_generation_files",
        sa.Column(
            "generation_id",
            sa.String(36),
            sa.ForeignKey("index_generations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "file_id",
            sa.Integer(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=True),
        sa.Column("source_updated_at", sa.TIMESTAMP(), nullable=False),
        sa.Column(
            "expected_chunk_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "written_chunk_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "expected_layer_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "written_layer_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("heartbeat_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.CheckConstraint(
            f"state IN ({_in_values(FILE_STATES)})",
            name="ck_index_generation_file_state",
        ),
        sa.CheckConstraint(
            "expected_chunk_count >= 0", name="ck_index_generation_file_expected_chunks"
        ),
        sa.CheckConstraint(
            "written_chunk_count >= 0", name="ck_index_generation_file_written_chunks"
        ),
        sa.CheckConstraint(
            "expected_layer_count >= 0", name="ck_index_generation_file_expected_layers"
        ),
        sa.CheckConstraint(
            "written_layer_count >= 0", name="ck_index_generation_file_written_layers"
        ),
        sa.CheckConstraint(
            "retry_count >= 0", name="ck_index_generation_file_retry_count"
        ),
    )
    op.create_index(
        "idx_index_generation_file_state_retry",
        "index_generation_files",
        ["generation_id", "state", "next_retry_at"],
    )
    op.create_index(
        "idx_index_generation_file_workspace",
        "index_generation_files",
        ["workspace_id", "generation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_index_generation_file_workspace", table_name="index_generation_files"
    )
    op.drop_index(
        "idx_index_generation_file_state_retry", table_name="index_generation_files"
    )
    op.drop_table("index_generation_files")
    op.drop_table("index_generation_routes")
    op.drop_index("idx_index_generation_scope_state", table_name="index_generations")
    op.drop_index("uq_index_generation_active_scope", table_name="index_generations")
    op.drop_table("index_generations")
