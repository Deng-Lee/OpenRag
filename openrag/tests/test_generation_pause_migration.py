import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _revision():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260715_0011_add_generation_build_pause.py"
    )
    spec = importlib.util.spec_from_file_location("generation_pause_revision", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_pause_migration_is_additive_and_reversible():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "index_generations",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        revision = _revision()
        revision.op = Operations(MigrationContext.configure(connection))
        revision.upgrade()
        assert "build_paused" in {
            column["name"]
            for column in sa.inspect(connection).get_columns("index_generations")
        }
        revision.downgrade()
        assert "build_paused" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("index_generations")
        }
