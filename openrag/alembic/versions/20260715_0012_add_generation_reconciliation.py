"""Add generation reconciliation progress fields.

Revision ID: 20260715_0012
Revises: 20260715_0011
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0012"
down_revision: Union[str, None] = "20260715_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.add_column(
            sa.Column("last_reconciled_at", sa.TIMESTAMP(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "build_lag_files", sa.BigInteger(), nullable=False, server_default="0"
            )
        )
        batch_op.create_check_constraint(
            "ck_index_generation_build_lag",
            "build_lag_files >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.drop_constraint(
            "ck_index_generation_build_lag",
            type_="check",
        )
        batch_op.drop_column("build_lag_files")
        batch_op.drop_column("last_reconciled_at")
