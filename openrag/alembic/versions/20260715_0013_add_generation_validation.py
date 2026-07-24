"""Add generation validation lifecycle fields.

Revision ID: 20260715_0013
Revises: 20260715_0012
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0013"
down_revision: Union[str, None] = "20260715_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.add_column(sa.Column("validation_started_at", sa.TIMESTAMP()))
        batch_op.add_column(sa.Column("validation_completed_at", sa.TIMESTAMP()))
        batch_op.add_column(sa.Column("validation_error_code", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.drop_column("validation_error_code")
        batch_op.drop_column("validation_completed_at")
        batch_op.drop_column("validation_started_at")
