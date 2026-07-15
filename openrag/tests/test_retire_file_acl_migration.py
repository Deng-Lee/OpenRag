"""PostgreSQL tests for retiring the file-level ACL schema."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

import openrag.config as config_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_ENV = "OPENRAG_TEST_POSTGRES_URL"


@pytest.fixture
def postgres_engine(monkeypatch) -> Engine:
    database_url = os.getenv(TEST_DATABASE_ENV)
    if not database_url:
        pytest.skip(f"{TEST_DATABASE_ENV} is not configured")

    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "postgresql":
        pytest.fail("R-02 migration tests require PostgreSQL")
    if parsed_url.database != "openrag_r02_test":
        pytest.fail("R-02 migration tests only run against database openrag_r02_test")

    monkeypatch.setenv("POSTGRES_HOST", parsed_url.host or "localhost")
    monkeypatch.setenv("POSTGRES_PORT", str(parsed_url.port or 5432))
    monkeypatch.setenv("POSTGRES_DATABASE", parsed_url.database)
    monkeypatch.setenv("POSTGRES_USER", parsed_url.username or "")
    monkeypatch.setenv("POSTGRES_PASSWORD", parsed_url.password or "")
    config_module._config = None

    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))

    yield engine

    engine.dispose()
    config_module._config = None


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def _run_alembic(action, revision: str) -> None:
    config_module._config = None
    action(_alembic_config(), revision)


def _prepare_schema(
    engine: Engine,
    *,
    include_acl: bool = True,
    shared_entity_type: bool = False,
) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE public.workspaces (id SERIAL PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE public.files (id SERIAL PRIMARY KEY)"))
        if not include_acl:
            return

        connection.execute(sa.text("CREATE TYPE public.entitytype AS ENUM ('USER', 'TEAM')"))
        connection.execute(
            sa.text("CREATE TYPE public.permission AS ENUM ('READ', 'WRITE', 'ADMIN')")
        )
        connection.execute(
            sa.text(
                """
                CREATE TABLE public.file_permissions (
                    id SERIAL PRIMARY KEY,
                    file_id INTEGER NOT NULL REFERENCES public.files(id),
                    workspace_id INTEGER NULL REFERENCES public.workspaces(id),
                    entity_type public.entitytype NOT NULL,
                    entity_id INTEGER NOT NULL,
                    permission public.permission NOT NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
                    CONSTRAINT uq_file_entity UNIQUE (file_id, entity_type, entity_id)
                )
                """
            )
        )
        connection.execute(
            sa.text(
                "CREATE INDEX idx_permission_file_id "
                "ON public.file_permissions (file_id)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE INDEX idx_permission_entity "
                "ON public.file_permissions (entity_type, entity_id)"
            )
        )
        if shared_entity_type:
            connection.execute(
                sa.text(
                    "CREATE TABLE public.shared_acl_entity "
                    "(id SERIAL PRIMARY KEY, entity_type public.entitytype NOT NULL)"
                )
            )


def _stamp_previous_revision() -> None:
    _run_alembic(command.stamp, "20260626_0005")


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.scalar(sa.text("SELECT version_num FROM alembic_version"))


def _regclass(engine: Engine, object_name: str):
    with engine.connect() as connection:
        return connection.scalar(
            sa.text("SELECT to_regclass(:object_name)"),
            {"object_name": object_name},
        )


def _regtype(engine: Engine, object_name: str):
    with engine.connect() as connection:
        return connection.scalar(
            sa.text("SELECT to_regtype(:object_name)"),
            {"object_name": object_name},
        )


def test_upgrade_drops_empty_acl_schema(postgres_engine: Engine):
    _prepare_schema(postgres_engine)
    _stamp_previous_revision()

    _run_alembic(command.upgrade, "head")

    assert _revision(postgres_engine) == "20260713_0006"
    assert _regclass(postgres_engine, "public.file_permissions") is None
    assert _regtype(postgres_engine, "public.entitytype") is None
    assert _regtype(postgres_engine, "public.permission") is None


def test_upgrade_aborts_and_preserves_nonempty_acl_schema(postgres_engine: Engine):
    _prepare_schema(postgres_engine)
    with postgres_engine.begin() as connection:
        workspace_id = connection.scalar(
            sa.text("INSERT INTO public.workspaces DEFAULT VALUES RETURNING id")
        )
        file_id = connection.scalar(
            sa.text("INSERT INTO public.files DEFAULT VALUES RETURNING id")
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO public.file_permissions
                    (file_id, workspace_id, entity_type, entity_id, permission)
                VALUES (:file_id, :workspace_id, 'USER', 99, 'READ')
                """
            ),
            {"file_id": file_id, "workspace_id": workspace_id},
        )
    _stamp_previous_revision()

    with pytest.raises(RuntimeError, match="contains rows"):
        _run_alembic(command.upgrade, "head")

    assert _revision(postgres_engine) == "20260626_0005"
    assert _regclass(postgres_engine, "public.file_permissions") is not None
    with postgres_engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT count(*) FROM public.file_permissions")
        ) == 1


def test_upgrade_handles_missing_acl_table(postgres_engine: Engine):
    _prepare_schema(postgres_engine, include_acl=False)
    _stamp_previous_revision()

    _run_alembic(command.upgrade, "head")

    assert _revision(postgres_engine) == "20260713_0006"
    assert _regclass(postgres_engine, "public.file_permissions") is None


def test_upgrade_preserves_shared_enum_types(postgres_engine: Engine):
    _prepare_schema(postgres_engine, shared_entity_type=True)
    _stamp_previous_revision()

    _run_alembic(command.upgrade, "head")

    assert _regclass(postgres_engine, "public.file_permissions") is None
    assert _regclass(postgres_engine, "public.shared_acl_entity") is not None
    assert _regtype(postgres_engine, "public.entitytype") is not None
    assert _regtype(postgres_engine, "public.permission") is None


def test_downgrade_restores_empty_writable_schema(postgres_engine: Engine):
    _prepare_schema(postgres_engine)
    _stamp_previous_revision()
    _run_alembic(command.upgrade, "head")

    _run_alembic(command.downgrade, "20260626_0005")

    assert _revision(postgres_engine) == "20260626_0005"
    assert _regclass(postgres_engine, "public.file_permissions") is not None
    inspector = sa.inspect(postgres_engine)
    columns = {column["name"]: column for column in inspector.get_columns("file_permissions")}
    assert set(columns) == {
        "id",
        "file_id",
        "workspace_id",
        "entity_type",
        "entity_id",
        "permission",
        "created_at",
    }
    assert columns["workspace_id"]["nullable"] is True
    assert all(
        columns[column_name]["nullable"] is False
        for column_name in (
            "id",
            "file_id",
            "entity_type",
            "entity_id",
            "permission",
            "created_at",
        )
    )
    assert columns["id"]["default"] is not None
    assert columns["created_at"]["default"] is not None

    unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("file_permissions")
    }
    indexes = {index["name"] for index in inspector.get_indexes("file_permissions")}
    foreign_keys = {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("file_permissions")
    }
    assert "uq_file_entity" in unique_constraints
    assert {"idx_permission_file_id", "idx_permission_entity"} <= indexes
    assert {("file_id",), ("workspace_id",)} <= foreign_keys

    with postgres_engine.begin() as connection:
        workspace_id = connection.scalar(
            sa.text("INSERT INTO public.workspaces DEFAULT VALUES RETURNING id")
        )
        file_id = connection.scalar(
            sa.text("INSERT INTO public.files DEFAULT VALUES RETURNING id")
        )
        inserted_ids = connection.execute(
            sa.text(
                """
                INSERT INTO public.file_permissions
                    (file_id, workspace_id, entity_type, entity_id, permission)
                VALUES
                    (:file_id, :workspace_id, 'USER', 100, 'READ'),
                    (:file_id, :workspace_id, 'TEAM', 200, 'WRITE')
                RETURNING id
                """
            ),
            {"file_id": file_id, "workspace_id": workspace_id},
        ).scalars().all()
    assert len(inserted_ids) == 2
    assert len(set(inserted_ids)) == 2


def test_upgrade_downgrade_upgrade_round_trip(postgres_engine: Engine):
    _prepare_schema(postgres_engine)
    _stamp_previous_revision()

    _run_alembic(command.upgrade, "head")
    _run_alembic(command.downgrade, "20260626_0005")
    _run_alembic(command.upgrade, "head")

    assert _revision(postgres_engine) == "20260713_0006"
    assert _regclass(postgres_engine, "public.file_permissions") is None
    assert _regtype(postgres_engine, "public.entitytype") is None
    assert _regtype(postgres_engine, "public.permission") is None
