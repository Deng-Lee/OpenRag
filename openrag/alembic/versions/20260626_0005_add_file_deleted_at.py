"""Add soft-delete marker deleted_at to files.

Revision ID: 20260626_0005
Revises: 20260626_0004
Create Date: 2026-06-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260626_0005"
down_revision: Union[str, None] = "20260626_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("files", sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True))
    op.create_index("idx_files_deleted_at", "files", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("idx_files_deleted_at", table_name="files")
    op.drop_column("files", "deleted_at")
