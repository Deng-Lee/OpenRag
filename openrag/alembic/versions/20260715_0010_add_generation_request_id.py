"""Add generation create idempotency key.

Revision ID: 20260715_0010
Revises: 20260715_0009
Create Date: 2026-07-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0010"
down_revision: Union[str, None] = "20260715_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.add_column(
            sa.Column("client_request_id", sa.String(128), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_index_generations_client_request_id", ["client_request_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("index_generations") as batch_op:
        batch_op.drop_constraint(
            "uq_index_generations_client_request_id", type_="unique"
        )
        batch_op.drop_column("client_request_id")
