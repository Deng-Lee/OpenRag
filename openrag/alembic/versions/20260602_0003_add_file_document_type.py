"""Add document_type to files.

Revision ID: 20260602_0003
Revises: 20260529_0002
Create Date: 2026-06-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260602_0003"
down_revision: Union[str, None] = "20260529_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column(
            "document_type",
            sa.String(length=32),
            nullable=False,
            server_default="general",
        ),
    )
    op.alter_column("files", "document_type", server_default=None)


def downgrade() -> None:
    op.drop_column("files", "document_type")
