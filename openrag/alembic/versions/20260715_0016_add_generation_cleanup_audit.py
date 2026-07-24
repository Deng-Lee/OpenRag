"""Add generation cleanup audit fields.

Revision ID: 20260715_0016
Revises: 20260715_0015
Create Date: 2026-07-15
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0016"
down_revision: Union[str, None] = "20260715_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.add_column(sa.Column("backup_id", sa.String(128)))
        batch_op.add_column(sa.Column("backup_status", sa.String(32)))
        batch_op.add_column(sa.Column("deletion_plan", sa.JSON()))
        batch_op.add_column(sa.Column("deleted_at", sa.TIMESTAMP()))
        batch_op.add_column(
            sa.Column("deleted_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"))
        )


def downgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.drop_column("deleted_by")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("deletion_plan")
        batch_op.drop_column("backup_status")
        batch_op.drop_column("backup_id")
