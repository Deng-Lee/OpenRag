"""Bind vector write tasks to one index generation.

Revision ID: 20260715_0009
Revises: 20260715_0008
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0009"
down_revision: Union[str, None] = "20260715_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column("index_generation_id", sa.String(36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_tasks_index_generation_id",
            "index_generations",
            ["index_generation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_tasks_index_generation_id", ["index_generation_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index("ix_tasks_index_generation_id")
        batch_op.drop_constraint("fk_tasks_index_generation_id", type_="foreignkey")
        batch_op.drop_column("index_generation_id")
