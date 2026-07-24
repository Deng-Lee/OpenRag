"""Add previous-generation mirror progress.

Revision ID: 20260715_0015
Revises: 20260715_0014
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0015"
down_revision: Union[str, None] = "20260715_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.add_column(
            sa.Column("mirror_lag_files", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("last_mirrored_at", sa.TIMESTAMP()))
        batch_op.create_check_constraint(
            "ck_index_generation_mirror_lag", "mirror_lag_files >= 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.drop_constraint("ck_index_generation_mirror_lag", type_="check")
        batch_op.drop_column("last_mirrored_at")
        batch_op.drop_column("mirror_lag_files")
