"""Additive migration smoke test for the generation registry."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def load_revision():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260715_0008_add_index_generations.py"
    )
    spec = importlib.util.spec_from_file_location("index_generation_revision", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_and_downgrade_are_additive():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    for name in ("users", "files", "workspaces"):
        sa.Table(name, metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        revision = load_revision()
        revision.op = Operations(MigrationContext.configure(connection))
        revision.upgrade()

        inspector = sa.inspect(connection)
        assert {
            "index_generations",
            "index_generation_routes",
            "index_generation_files",
        }.issubset(inspector.get_table_names())
        assert "files" in inspector.get_table_names()
        assert {
            index["name"] for index in inspector.get_indexes("index_generations")
        } >= {
            "idx_index_generation_scope_state",
            "uq_index_generation_active_scope",
        }

        revision.downgrade()
        inspector = sa.inspect(connection)
        assert "index_generations" not in inspector.get_table_names()
        assert "files" in inspector.get_table_names()
