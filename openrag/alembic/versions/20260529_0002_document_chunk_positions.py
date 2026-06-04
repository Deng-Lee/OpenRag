"""Add RAGFlow position fields to document_chunks.

Revision ID: 20260529_0002
Revises: 20260527_0001
Create Date: 2026-05-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260529_0002"
down_revision: Union[str, None] = "20260527_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("page_num_int", _json_type(), nullable=True))
    op.add_column("document_chunks", sa.Column("position_int", _json_type(), nullable=True))
    op.add_column("document_chunks", sa.Column("top_int", _json_type(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_chunks", "top_int")
    op.drop_column("document_chunks", "position_int")
    op.drop_column("document_chunks", "page_num_int")
