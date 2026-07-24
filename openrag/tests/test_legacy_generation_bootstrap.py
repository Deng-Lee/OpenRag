"""Legacy Collection registration is explicit, read-only and idempotent."""

from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.config import EmbeddingConfig
from openrag.indexing.legacy_bootstrap import (
    LegacyBootstrapError,
    LegacyCollectionSnapshot,
    bootstrap_legacy_generation,
    print_legacy_bootstrap_dry_run,
    validate_active_route_on_startup,
)
from openrag.models import Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute

CHUNK_FIELDS = (
    {"name": "chunk_id", "type": "VARCHAR"},
    {"name": "file_id", "type": "INT64"},
    {"name": "text", "type": "VARCHAR"},
    {"name": "embedding", "type": "FLOAT_VECTOR", "dim": 3},
    {"name": "page", "type": "INT64"},
    {"name": "level", "type": "INT64"},
    {"name": "block_type", "type": "VARCHAR"},
)
LAYER_FIELDS = (
    {"name": "layer_row_id", "type": "VARCHAR"},
    {"name": "file_id", "type": "INT64"},
    {"name": "layer", "type": "VARCHAR"},
    {"name": "text", "type": "VARCHAR"},
    {"name": "embedding", "type": "FLOAT_VECTOR", "dim": 3},
)


class FakeInspector:
    def __init__(self, *, chunk=None, layer=None):
        self.snapshots = {
            "openrag_chunks": chunk
            or LegacyCollectionSnapshot(
                name="openrag_chunks",
                fields=CHUNK_FIELDS,
                indexes=({"field_name": "embedding", "metric_type": "COSINE"},),
                entity_count=12,
                aliases=("chunks_external",),
            ),
            "openrag_layers": layer
            or LegacyCollectionSnapshot(
                name="openrag_layers",
                fields=LAYER_FIELDS,
                indexes=({"field_name": "embedding", "metric_type": "COSINE"},),
                entity_count=4,
                aliases=("layers_external",),
            ),
        }
        self.calls = []

    def inspect_collection(self, name):
        self.calls.append(name)
        snapshot = self.snapshots.get(name)
        if snapshot is None:
            raise LegacyBootstrapError("LEGACY_COLLECTION_MISSING", f"{name} missing")
        return snapshot


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def embedding_config(**overrides):
    values = {
        "provider": "openai",
        "model": "legacy-model",
        "dimension": 3,
        "batch_size": 8,
        "request_timeout_seconds": 60,
        "max_attempts": 3,
        "probe_interval_seconds": 30,
    }
    values.update(overrides)
    return EmbeddingConfig(**values)


def test_dry_run_reads_collections_without_writing_registry(db):
    inspector = FakeInspector()

    output = print_legacy_bootstrap_dry_run(
        db,
        inspector=inspector,
        embedding_config=embedding_config(),
        operator="tester",
    )

    assert db.query(IndexGeneration).count() == 0
    assert db.query(IndexGenerationRoute).count() == 0
    assert inspector.calls == ["openrag_chunks", "openrag_layers"]
    assert '"identity_confidence": "declared"' in output
    assert "secret" not in output.lower()


def test_bootstrap_registers_existing_physical_names_and_counts(db):
    result = bootstrap_legacy_generation(
        db,
        inspector=FakeInspector(),
        embedding_config=embedding_config(),
        operator="tester",
    )

    generation = db.get(IndexGeneration, result.generation_id)
    route = db.get(IndexGenerationRoute, "global")
    assert result.created is True
    assert generation.state == "active"
    assert generation.chunk_collection_name == "openrag_chunks"
    assert generation.layer_collection_name == "openrag_layers"
    assert generation.indexed_chunk_count == 12
    assert generation.indexed_layer_count == 4
    assert generation.manifest["legacy"] is True
    assert generation.manifest["identity_confidence"] == "declared"
    assert route.active_generation_id == generation.id


def test_repeated_bootstrap_is_idempotent(db):
    inspector = FakeInspector()
    first = bootstrap_legacy_generation(
        db, inspector=inspector, embedding_config=embedding_config(), operator="tester"
    )
    second = bootstrap_legacy_generation(
        db, inspector=inspector, embedding_config=embedding_config(), operator="tester"
    )

    assert second.generation_id == first.generation_id
    assert second.created is False
    assert db.query(IndexGeneration).count() == 1
    assert db.query(IndexGenerationRoute).count() == 1


def test_bootstrap_rejects_wrong_vector_dimension_without_registry_write(db):
    bad_chunk = replace(
        FakeInspector().snapshots["openrag_chunks"],
        fields=tuple(
            {**field, "dim": 2} if field["name"] == "embedding" else field
            for field in CHUNK_FIELDS
        ),
    )

    with pytest.raises(LegacyBootstrapError) as exc_info:
        bootstrap_legacy_generation(
            db,
            inspector=FakeInspector(chunk=bad_chunk),
            embedding_config=embedding_config(),
            operator="tester",
        )

    assert exc_info.value.code == "LEGACY_SCHEMA_MISMATCH"
    assert db.query(IndexGeneration).count() == 0


def test_explicit_revision_and_identity_are_marked_verified(db):
    result = bootstrap_legacy_generation(
        db,
        inspector=FakeInspector(),
        embedding_config=embedding_config(
            revision="sha256:revision", model_identity="registry/model@sha256:revision"
        ),
        operator="tester",
    )

    generation = db.get(IndexGeneration, result.generation_id)
    assert generation.manifest["identity_confidence"] == "verified"


def test_startup_validation_is_read_only(db):
    result = bootstrap_legacy_generation(
        db,
        inspector=FakeInspector(),
        embedding_config=embedding_config(),
        operator="tester",
    )
    before = db.query(IndexGeneration).count()

    snapshot = validate_active_route_on_startup(db)

    assert snapshot["active_generation_id"] == result.generation_id
    assert db.query(IndexGeneration).count() == before
