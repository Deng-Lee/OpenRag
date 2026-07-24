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
        / "20260715_0009_add_task_index_generation.py"
    )
    spec = importlib.util.spec_from_file_location("task_generation_revision", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_generation_migration_upgrade_and_downgrade():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "index_generations",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table("tasks", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        revision = _revision()
        revision.op = Operations(MigrationContext.configure(connection))
        revision.upgrade()
        inspector = sa.inspect(connection)
        assert "index_generation_id" in {
            column["name"] for column in inspector.get_columns("tasks")
        }
        assert "ix_tasks_index_generation_id" in {
            index["name"] for index in inspector.get_indexes("tasks")
        }

        revision.downgrade()
        assert "index_generation_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns("tasks")
        }
