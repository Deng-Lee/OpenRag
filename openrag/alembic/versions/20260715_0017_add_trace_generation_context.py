"""Add explicit generation context to traces.

Revision ID: 20260715_0017
Revises: 20260715_0016
Create Date: 2026-07-15
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0017"
down_revision: Union[str, None] = "20260715_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trace_runs") as batch_op:
        batch_op.add_column(sa.Column("index_generation_id", sa.String(36)))
        batch_op.add_column(sa.Column("route_version", sa.Integer()))
        batch_op.add_column(sa.Column("embedding_fingerprint", sa.String(64)))
        batch_op.add_column(sa.Column("embedding_revision", sa.String(256)))
        batch_op.add_column(sa.Column("collection_role", sa.String(32)))
        batch_op.add_column(sa.Column("collection_name", sa.String(255)))


def downgrade() -> None:
    with op.batch_alter_table("trace_runs") as batch_op:
        for name in (
            "collection_name",
            "collection_role",
            "embedding_revision",
            "embedding_fingerprint",
            "route_version",
            "index_generation_id",
        ):
            batch_op.drop_column(name)
