"""Add per-workspace unique tag to files.

Revision ID: 20260626_0004
Revises: 20260602_0003
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260626_0004"
down_revision: Union[str, None] = "20260602_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("files", sa.Column("tag", sa.String(length=128), nullable=True))
    op.create_unique_constraint("uq_files_workspace_tag", "files", ["workspace_id", "tag"])


def downgrade() -> None:
    op.drop_constraint("uq_files_workspace_tag", "files", type_="unique")
    op.drop_column("files", "tag")
