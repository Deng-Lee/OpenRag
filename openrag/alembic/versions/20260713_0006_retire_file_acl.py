"""Retire the unused file-level ACL schema.

Revision ID: 20260713_0006
Revises: 20260626_0005
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260713_0006"
down_revision: Union[str, None] = "20260626_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCHEMA = "public"
_TABLE = "file_permissions"


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _require_postgresql(bind: sa.Connection) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError("R-02 file ACL migration requires PostgreSQL")


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    table_name = f"{_SCHEMA}.{_TABLE}"
    table_exists = bind.scalar(
        sa.text("SELECT to_regclass(:table_name) IS NOT NULL"),
        {"table_name": table_name},
    )
    if not table_exists:
        return

    op.execute(
        sa.text(
            f"LOCK TABLE {_quoted(_SCHEMA)}.{_quoted(_TABLE)} "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    has_rows = bind.scalar(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {_quoted(_SCHEMA)}.{_quoted(_TABLE)} LIMIT 1)"
        )
    )
    if has_rows:
        raise RuntimeError(
            "R-02 migration aborted: public.file_permissions contains rows; "
            "review and migrate callers/data manually before retrying"
        )

    enum_types = bind.execute(
        sa.text(
            """
            SELECT DISTINCT type_ns.nspname, type_info.typname
            FROM pg_attribute AS attr
            JOIN pg_class AS table_info ON table_info.oid = attr.attrelid
            JOIN pg_namespace AS table_ns ON table_ns.oid = table_info.relnamespace
            JOIN pg_type AS type_info ON type_info.oid = attr.atttypid
            JOIN pg_namespace AS type_ns ON type_ns.oid = type_info.typnamespace
            WHERE table_ns.nspname = :schema_name
              AND table_info.relname = :table_name
              AND attr.attname IN ('entity_type', 'permission')
              AND attr.attnum > 0
              AND NOT attr.attisdropped
              AND type_info.typtype = 'e'
            """
        ),
        {"schema_name": _SCHEMA, "table_name": _TABLE},
    ).all()

    op.drop_table(_TABLE, schema=_SCHEMA)

    for type_schema, type_name in enum_types:
        qualified_type = f"{_quoted(type_schema)}.{_quoted(type_name)}"
        type_oid = bind.scalar(
            sa.text("SELECT to_regtype(:type_name)::oid"),
            {"type_name": qualified_type},
        )
        if type_oid is None:
            continue

        has_external_dependencies = bind.scalar(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_depend
                    WHERE refclassid = 'pg_type'::regclass
                      AND refobjid = :type_oid
                      AND deptype <> 'i'
                )
                """
            ),
            {"type_oid": type_oid},
        )
        if not has_external_dependencies:
            op.execute(sa.text(f"DROP TYPE {qualified_type}"))


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    entity_type = postgresql.ENUM(
        "USER",
        "TEAM",
        name="entitytype",
        schema=_SCHEMA,
        create_type=False,
    )
    permission = postgresql.ENUM(
        "READ",
        "WRITE",
        "ADMIN",
        name="permission",
        schema=_SCHEMA,
        create_type=False,
    )

    for enum_name, values in (
        ("entitytype", ("USER", "TEAM")),
        ("permission", ("READ", "WRITE", "ADMIN")),
    ):
        existing_values = bind.execute(
            sa.text(
                """
                SELECT enum_value.enumlabel
                FROM pg_type AS type_info
                JOIN pg_namespace AS type_ns ON type_ns.oid = type_info.typnamespace
                JOIN pg_enum AS enum_value ON enum_value.enumtypid = type_info.oid
                WHERE type_ns.nspname = :schema_name
                  AND type_info.typname = :type_name
                ORDER BY enum_value.enumsortorder
                """
            ),
            {"schema_name": _SCHEMA, "type_name": enum_name},
        ).scalars().all()
        if not existing_values:
            postgresql.ENUM(
                *values,
                name=enum_name,
                schema=_SCHEMA,
            ).create(bind, checkfirst=True)
        elif not set(values).issubset(existing_values):
            raise RuntimeError(
                f"Cannot restore file_permissions: {_SCHEMA}.{enum_name} "
                f"does not contain required labels {values}"
            )

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "file_id",
            sa.Integer(),
            sa.ForeignKey("public.files.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("public.workspaces.id"),
            nullable=True,
        ),
        sa.Column("entity_type", entity_type, nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("permission", permission, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "file_id",
            "entity_type",
            "entity_id",
            name="uq_file_entity",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_permission_file_id",
        _TABLE,
        ["file_id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "idx_permission_entity",
        _TABLE,
        ["entity_type", "entity_id"],
        schema=_SCHEMA,
    )
