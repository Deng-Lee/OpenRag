"""Add persistent candidate build pause flag.

Revision ID: 20260715_0011
Revises: 20260715_0010
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0011"
down_revision: Union[str, None] = "20260715_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "index_generations",
        sa.Column("build_paused", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("index_generations", "build_paused")
