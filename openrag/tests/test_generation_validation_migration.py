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
        / "20260715_0013_add_generation_validation.py"
    )
    spec = importlib.util.spec_from_file_location("generation_validation_revision", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generation_validation_migration_is_additive_and_reversible():
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
        columns = {
            item["name"]
            for item in sa.inspect(connection).get_columns("index_generations")
        }
        assert {
            "validation_started_at",
            "validation_completed_at",
            "validation_error_code",
        } <= columns
        revision.downgrade()
        columns = {
            item["name"]
            for item in sa.inspect(connection).get_columns("index_generations")
        }
        assert "validation_started_at" not in columns
