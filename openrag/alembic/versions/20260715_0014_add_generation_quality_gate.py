"""Add generation quality gate fields.

Revision ID: 20260715_0014
Revises: 20260715_0013
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0014"
down_revision: Union[str, None] = "20260715_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.add_column(sa.Column("quality_gate_passed", sa.Boolean()))
        batch_op.add_column(sa.Column("quality_validated_at", sa.TIMESTAMP()))


def downgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.drop_column("quality_validated_at")
        batch_op.drop_column("quality_gate_passed")
