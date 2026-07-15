"""Add delayed retry metadata to tasks.

Revision ID: 20260715_0007
Revises: 20260713_0006
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0007"
down_revision: Union[str, None] = "20260713_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("error_code", sa.String(64), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "error_retryable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("tasks", sa.Column("next_retry_at", sa.TIMESTAMP(), nullable=True))
    op.create_index(
        "idx_task_ready_retry",
        "tasks",
        ["status", "next_retry_at", "priority", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_task_ready_retry", table_name="tasks")
    op.drop_column("tasks", "next_retry_at")
    op.drop_column("tasks", "error_retryable")
    op.drop_column("tasks", "error_code")
