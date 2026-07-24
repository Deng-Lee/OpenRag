"""A request resolves one immutable generation runtime snapshot."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.runtime import (
    IndexRouteUnavailableError,
    IndexRuntime,
    IndexRuntimeResolver,
)
from openrag.models import Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute
from openrag.config import EmbeddingConfig


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


def add_generation(db, suffix, *, state="active"):
    generation = IndexGeneration(
        id=f"generation-{suffix}",
        scope="global",
        state=state,
        embedding_provider="openai",
        embedding_model=f"model-{suffix}",
        embedding_revision=f"revision-{suffix}",
        embedding_dimension=3,
        embedding_fingerprint=suffix * 64,
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=1,
        chunk_policy_revision="chunks-v1",
        hierarchy_policy_revision="hierarchy-v1",
        chunk_collection_name=f"chunks_{suffix}",
        layer_collection_name=f"layers_{suffix}",
        manifest={"embedding": {"provider": "openai", "model": f"model-{suffix}"}},
    )
    db.add(generation)
    db.flush()
    return generation


def test_missing_route_fails_without_legacy_fallback(db):
    with pytest.raises(IndexRouteUnavailableError):
        IndexRuntimeResolver().get_active_snapshot(db)


def test_active_snapshot_binds_chunk_layer_and_fingerprint(db):
    generation = add_generation(db, "a")
    db.add(
        IndexGenerationRoute(
            scope="global", active_generation_id=generation.id, route_version=7
        )
    )
    db.commit()

    snapshot = IndexRuntimeResolver().get_active_snapshot(db)

    assert snapshot.generation_id == generation.id
    assert snapshot.route_version == 7
    assert snapshot.embedding_fingerprint == "a" * 64
    assert snapshot.chunk_collection_name == "chunks_a"
    assert snapshot.layer_collection_name == "layers_a"


def test_route_switch_does_not_mutate_existing_snapshot(db):
    old = add_generation(db, "a")
    candidate = add_generation(db, "b", state="ready")
    route = IndexGenerationRoute(
        scope="global", active_generation_id=old.id, route_version=1
    )
    db.add(route)
    db.commit()
    resolver = IndexRuntimeResolver()
    old_snapshot = resolver.get_active_snapshot(db)

    old.state = "retired"
    candidate.state = "active"
    route.active_generation_id = candidate.id
    route.previous_generation_id = old.id
    route.route_version = 2
    db.commit()
    new_snapshot = resolver.get_active_snapshot(db)

    assert old_snapshot.generation_id == old.id
    assert old_snapshot.chunk_collection_name == "chunks_a"
    assert new_snapshot.generation_id == candidate.id
    assert new_snapshot.chunk_collection_name == "chunks_b"


def test_runtime_cache_key_includes_generation_and_fingerprint(db):
    generation = add_generation(db, "a")
    db.add(
        IndexGenerationRoute(
            scope="global", active_generation_id=generation.id, route_version=1
        )
    )
    db.commit()
    built = []

    def builder(snapshot):
        built.append(snapshot.embedding_fingerprint)
        return IndexRuntime(
            snapshot=snapshot,
            embedding_engine=object(),
            vector_store=object(),
            layer_store=object(),
        )

    resolver = IndexRuntimeResolver(runtime_builder=builder)
    snapshot = resolver.get_active_snapshot(db)
    first = resolver.get_runtime(snapshot)
    second = resolver.get_runtime(snapshot)

    assert first is second
    assert built == ["a" * 64]


def test_runtime_engine_uses_candidate_manifest_identity(monkeypatch, db):
    generation = add_generation(db, "a", state="building")
    generation.manifest = {
        "embedding": {
            "provider": "openai",
            "model": "candidate-model",
            "model_revision": "candidate-revision",
            "model_identity": "candidate-digest",
            "dimension": 3,
            "input_type": "document",
            "encoding_format": "float",
            "normalization": "none",
            "distance_metric": "COSINE",
            "query_prefix_revision": "query-v2",
            "document_prefix_revision": "document-v2",
            "text_preprocess_revision": "text-v2",
            "sdk_contract_revision": "sdk-v2",
        }
    }
    db.commit()
    captured = {}

    class Engine:
        def __init__(self, config):
            captured["config"] = config

        def assert_fingerprint(self, fingerprint):
            captured["fingerprint"] = fingerprint

    class Store:
        def __init__(self, collection_name, **_kwargs):
            self.collection_name = collection_name

    monkeypatch.setattr("openrag.indexing.runtime.EmbeddingEngine", Engine)
    monkeypatch.setattr("openrag.indexing.runtime.MilvusStore", Store)
    monkeypatch.setattr("openrag.indexing.runtime.MilvusLayerStore", Store)
    base = EmbeddingConfig(
        provider="openai",
        model="active-model",
        revision="active-revision",
        dimension=3,
        api_key="shared-secret",
    )
    resolver = IndexRuntimeResolver(embedding_config_loader=lambda _ref: base)

    runtime = resolver.get_runtime(resolver.get_generation_snapshot(db, generation.id))

    assert runtime.embedding_engine is not None
    assert captured["config"].model == "candidate-model"
    assert captured["config"].revision == "candidate-revision"
    assert captured["config"].model_identity == "candidate-digest"
    assert captured["config"].api_key.get_secret_value() == "shared-secret"
    assert captured["fingerprint"] == "a" * 64
