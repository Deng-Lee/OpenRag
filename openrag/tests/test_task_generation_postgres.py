"""Opt-in PostgreSQL DDL test for task generation binding."""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from openrag.database import get_database_url


def _revision(filename: str, module_name: str):
    path = Path(__file__).parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    os.getenv("OPENRAG_RUN_POSTGRES_MIGRATION_TESTS") != "1",
    reason="set OPENRAG_RUN_POSTGRES_MIGRATION_TESTS=1 to run local PostgreSQL DDL tests",
)
def test_postgres_task_generation_fk_upgrade_and_downgrade():
    url = make_url(get_database_url())
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("destructive DDL integration test is restricted to localhost")

    engine = sa.create_engine(url)
    schema = f"r05_task_test_{uuid4().hex}"
    quoted_schema = engine.dialect.identifier_preparer.quote(schema)
    generation_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
            connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
            for table in ("users", "files", "workspaces"):
                connection.execute(
                    sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                )
            connection.execute(sa.text("CREATE TABLE tasks (id INTEGER PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE trace_runs (id INTEGER PRIMARY KEY)"))
            context = MigrationContext.configure(connection)
            revision_8 = _revision(
                "20260715_0008_add_index_generations.py", "generation_revision_pg"
            )
            revision_8.op = Operations(context)
            revision_8.upgrade()
            revision_9 = _revision(
                "20260715_0009_add_task_index_generation.py", "task_revision_pg"
            )
            revision_9.op = Operations(context)
            revision_9.upgrade()
            revision_10 = _revision(
                "20260715_0010_add_generation_request_id.py", "request_revision_pg"
            )
            revision_10.op = Operations(context)
            revision_10.upgrade()
            revision_11 = _revision(
                "20260715_0011_add_generation_build_pause.py", "pause_revision_pg"
            )
            revision_11.op = Operations(context)
            revision_11.upgrade()
            revision_12 = _revision(
                "20260715_0012_add_generation_reconciliation.py",
                "reconciliation_revision_pg",
            )
            revision_12.op = Operations(context)
            revision_12.upgrade()
            revision_13 = _revision(
                "20260715_0013_add_generation_validation.py",
                "validation_revision_pg",
            )
            revision_13.op = Operations(context)
            revision_13.upgrade()
            revision_14 = _revision(
                "20260715_0014_add_generation_quality_gate.py",
                "quality_revision_pg",
            )
            revision_14.op = Operations(context)
            revision_14.upgrade()
            revision_15 = _revision(
                "20260715_0015_add_generation_mirror_progress.py",
                "mirror_revision_pg",
            )
            revision_15.op = Operations(context)
            revision_15.upgrade()
            revision_16 = _revision(
                "20260715_0016_add_generation_cleanup_audit.py",
                "cleanup_revision_pg",
            )
            revision_16.op = Operations(context)
            revision_16.upgrade()
            revision_17 = _revision(
                "20260715_0017_add_trace_generation_context.py",
                "trace_generation_revision_pg",
            )
            revision_17.op = Operations(context)
            revision_17.upgrade()
            connection.execute(
                sa.text("""
                    INSERT INTO index_generations (
                        id, scope, state, embedding_provider, embedding_model,
                        embedding_revision, embedding_dimension, embedding_fingerprint,
                        embedding_config_ref, vector_normalization, distance_metric,
                        schema_version, chunk_policy_revision, hierarchy_policy_revision,
                        chunk_collection_name, manifest
                    ) VALUES (
                        :id, 'global', 'active', 'provider', 'model', 'revision', 3,
                        :fingerprint, 'embedding/default', 'none', 'COSINE', 2,
                        'chunk-v1', 'hierarchy-v1', 'chunks_a', '{}'::json
                    )
                    """),
                {"id": generation_id, "fingerprint": "a" * 64},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO tasks (id, index_generation_id) VALUES (1, :generation_id)"
                ),
                {"generation_id": generation_id},
            )
            connection.execute(
                sa.text(
                    "UPDATE index_generations SET client_request_id = 'request-one' WHERE id = :id"
                ),
                {"id": generation_id},
            )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
                connection.execute(
                    sa.text(
                        "INSERT INTO tasks (id, index_generation_id) VALUES (2, :generation_id)"
                    ),
                    {"generation_id": "missing-generation"},
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO index_generations (
                            id, client_request_id, scope, state, embedding_provider,
                            embedding_model, embedding_revision, embedding_dimension,
                            embedding_fingerprint, embedding_config_ref,
                            vector_normalization, distance_metric, schema_version,
                            chunk_policy_revision, hierarchy_policy_revision,
                            chunk_collection_name, manifest
                        ) VALUES (
                            'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'request-one',
                            'global', 'draft', 'provider', 'model', 'revision', 3,
                            :fingerprint, 'embedding/default', 'none', 'COSINE', 2,
                            'chunk-v1', 'hierarchy-v1', 'chunks_b', '{}'::json
                        )
                        """
                    ),
                    {"fingerprint": "b" * 64},
                )

        with engine.begin() as connection:
            connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
            revision_17 = _revision(
                "20260715_0017_add_trace_generation_context.py",
                "trace_generation_revision_pg_down",
            )
            revision_17.op = Operations(MigrationContext.configure(connection))
            revision_17.downgrade()
            revision_16 = _revision(
                "20260715_0016_add_generation_cleanup_audit.py",
                "cleanup_revision_pg_down",
            )
            revision_16.op = Operations(MigrationContext.configure(connection))
            revision_16.downgrade()
            revision_15 = _revision(
                "20260715_0015_add_generation_mirror_progress.py",
                "mirror_revision_pg_down",
            )
            revision_15.op = Operations(MigrationContext.configure(connection))
            revision_15.downgrade()
            revision_14 = _revision(
                "20260715_0014_add_generation_quality_gate.py",
                "quality_revision_pg_down",
            )
            revision_14.op = Operations(MigrationContext.configure(connection))
            revision_14.downgrade()
            revision_13 = _revision(
                "20260715_0013_add_generation_validation.py",
                "validation_revision_pg_down",
            )
            revision_13.op = Operations(MigrationContext.configure(connection))
            revision_13.downgrade()
            revision_12 = _revision(
                "20260715_0012_add_generation_reconciliation.py",
                "reconciliation_revision_pg_down",
            )
            revision_12.op = Operations(MigrationContext.configure(connection))
            revision_12.downgrade()
            revision_11 = _revision(
                "20260715_0011_add_generation_build_pause.py",
                "pause_revision_pg_down",
            )
            revision_11.op = Operations(MigrationContext.configure(connection))
            revision_11.downgrade()
            revision_10 = _revision(
                "20260715_0010_add_generation_request_id.py",
                "request_revision_pg_down",
            )
            revision_10.op = Operations(MigrationContext.configure(connection))
            revision_10.downgrade()
            revision_9 = _revision(
                "20260715_0009_add_task_index_generation.py", "task_revision_pg_down"
            )
            revision_9.op = Operations(MigrationContext.configure(connection))
            revision_9.downgrade()
            assert "index_generation_id" not in {
                column["name"] for column in sa.inspect(connection).get_columns("tasks")
            }
    finally:
        with engine.begin() as connection:
            connection.execute(
                sa.text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            )
        engine.dispose()
