import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def revision():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260715_0010_add_generation_request_id.py"
    )
    spec = importlib.util.spec_from_file_location("generation_request_revision", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_request_id_migration_is_additive_and_reversible():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "index_generations",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        item = revision()
        item.op = Operations(MigrationContext.configure(connection))
        item.upgrade()
        assert "client_request_id" in {
            column["name"]
            for column in sa.inspect(connection).get_columns("index_generations")
        }
        item.downgrade()
        assert "client_request_id" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("index_generations")
        }
