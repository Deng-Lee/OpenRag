"""Opt-in PostgreSQL migration and partial-unique-index integration test."""

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


def load_revision():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260715_0008_add_index_generations.py"
    )
    spec = importlib.util.spec_from_file_location("index_generation_pg_revision", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    os.getenv("OPENRAG_RUN_POSTGRES_MIGRATION_TESTS") != "1",
    reason="set OPENRAG_RUN_POSTGRES_MIGRATION_TESTS=1 to run local PostgreSQL DDL tests",
)
def test_postgres_upgrade_unique_active_and_downgrade():
    url = make_url(get_database_url())
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("destructive DDL integration test is restricted to localhost")

    engine = sa.create_engine(url)
    schema = f"r05_test_{uuid4().hex}"
    quoted_schema = engine.dialect.identifier_preparer.quote(schema)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))

        with engine.begin() as connection:
            connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
            for table in ("users", "files", "workspaces"):
                connection.execute(
                    sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                )
            revision = load_revision()
            revision.op = Operations(MigrationContext.configure(connection))
            revision.upgrade()

        required = {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "scope": "global",
            "state": "active",
            "embedding_provider": "openai",
            "embedding_model": "model",
            "embedding_revision": "revision",
            "embedding_dimension": 3,
            "embedding_fingerprint": "a" * 64,
            "embedding_config_ref": "embedding/default",
            "vector_normalization": "none",
            "distance_metric": "COSINE",
            "schema_version": 1,
            "chunk_policy_revision": "chunks-v1",
            "hierarchy_policy_revision": "hierarchy-v1",
            "chunk_collection_name": "chunks_a",
            "layer_collection_name": "layers_a",
        }
        insert_sql = sa.text("""
            INSERT INTO index_generations (
                id, scope, state, embedding_provider, embedding_model,
                embedding_revision, embedding_dimension, embedding_fingerprint,
                embedding_config_ref, vector_normalization, distance_metric,
                schema_version, chunk_policy_revision, hierarchy_policy_revision,
                chunk_collection_name, layer_collection_name, manifest
            ) VALUES (
                :id, :scope, :state, :embedding_provider, :embedding_model,
                :embedding_revision, :embedding_dimension, :embedding_fingerprint,
                :embedding_config_ref, :vector_normalization, :distance_metric,
                :schema_version, :chunk_policy_revision, :hierarchy_policy_revision,
                :chunk_collection_name, :layer_collection_name, '{}'::json
            )
            """)
        with engine.begin() as connection:
            connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
            connection.execute(insert_sql, required)

        duplicate = dict(required)
        duplicate.update(
            id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            embedding_fingerprint="b" * 64,
            chunk_collection_name="chunks_b",
            layer_collection_name="layers_b",
        )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
                connection.execute(insert_sql, duplicate)

        with engine.begin() as connection:
            connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
            revision = load_revision()
            revision.op = Operations(MigrationContext.configure(connection))
            revision.downgrade()
            tables = set(sa.inspect(connection).get_table_names(schema=schema))
            assert "index_generations" not in tables
            assert {"users", "files", "workspaces"}.issubset(tables)
    finally:
        with engine.begin() as connection:
            connection.execute(
                sa.text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            )
        engine.dispose()
