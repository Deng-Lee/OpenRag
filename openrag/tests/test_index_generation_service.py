"""Index generation state transitions and optimistic route switching."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base
from openrag.models.index_generation import (
    IndexGenerationRoute,
    IndexGenerationState,
)
from openrag.services.index_generation_service import (
    GenerationImmutableError,
    GenerationStateTransitionError,
    IndexGenerationService,
    RouteVersionConflictError,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def create_generation(service: IndexGenerationService, suffix: str = "a"):
    return service.create_generation(
        scope="global",
        embedding_provider="openai",
        embedding_model="embedding-model",
        embedding_revision="revision-1",
        embedding_dimension=3,
        embedding_fingerprint=suffix * 64,
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=1,
        chunk_policy_revision="chunks-v1",
        hierarchy_policy_revision="hierarchy-v1",
        chunk_collection_name=f"openrag_chunks_g_{suffix * 12}",
        layer_collection_name=f"openrag_layers_g_{suffix * 12}",
        manifest={"model_identity": f"digest-{suffix}"},
    )


def test_create_generation_only_creates_draft(db):
    item = create_generation(IndexGenerationService(db))

    assert item.state == IndexGenerationState.DRAFT.value
    assert item.lock_version == 0


def test_create_generation_accepts_only_explicit_draft_state(db):
    service = IndexGenerationService(db)
    values = {
        "scope": "global",
        "embedding_provider": "openai",
        "embedding_model": "embedding-model",
        "embedding_revision": "revision-1",
        "embedding_dimension": 3,
        "embedding_fingerprint": "a" * 64,
        "embedding_config_ref": "embedding/default",
        "vector_normalization": "none",
        "distance_metric": "COSINE",
        "schema_version": 1,
        "chunk_policy_revision": "chunks-v1",
        "hierarchy_policy_revision": "hierarchy-v1",
        "chunk_collection_name": "openrag_chunks_g_explicit",
        "layer_collection_name": "openrag_layers_g_explicit",
        "manifest": {},
    }

    item = service.create_generation(state=IndexGenerationState.DRAFT, **values)
    assert item.state == IndexGenerationState.DRAFT.value

    values["chunk_collection_name"] = "openrag_chunks_g_invalid"
    values["layer_collection_name"] = "openrag_layers_g_invalid"
    with pytest.raises(GenerationStateTransitionError):
        service.create_generation(state=IndexGenerationState.ACTIVE, **values)


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (IndexGenerationState.DRAFT, IndexGenerationState.ACTIVE),
        (IndexGenerationState.FAILED, IndexGenerationState.ACTIVE),
        (IndexGenerationState.ACTIVE, IndexGenerationState.BUILDING),
        (IndexGenerationState.DELETED, IndexGenerationState.DRAFT),
    ],
)
def test_illegal_state_transitions_are_rejected(db, initial, target):
    service = IndexGenerationService(db)
    item = create_generation(service)
    item.state = initial.value
    db.commit()

    with pytest.raises(GenerationStateTransitionError):
        service.transition_state(item.id, target)


def test_legal_state_transition_increments_lock_version(db):
    service = IndexGenerationService(db)
    item = create_generation(service)

    updated = service.transition_state(item.id, IndexGenerationState.PROVISIONING)

    assert updated.state == IndexGenerationState.PROVISIONING.value
    assert updated.lock_version == 1


def test_manifest_fields_are_immutable_after_draft(db):
    service = IndexGenerationService(db)
    item = create_generation(service)
    service.transition_state(item.id, IndexGenerationState.PROVISIONING)

    with pytest.raises(GenerationImmutableError):
        service.update_manifest_fields(item.id, embedding_fingerprint="b" * 64)


def test_compare_and_swap_route_rejects_stale_version(db):
    service = IndexGenerationService(db)
    active = create_generation(service, "a")
    candidate = create_generation(service, "b")
    active.state = IndexGenerationState.ACTIVE.value
    candidate.state = IndexGenerationState.READY.value
    db.add(
        IndexGenerationRoute(
            scope="global",
            active_generation_id=active.id,
            route_version=4,
        )
    )
    db.commit()

    with pytest.raises(RouteVersionConflictError):
        service.compare_and_swap_route(
            scope="global",
            expected_route_version=3,
            active_generation_id=candidate.id,
            previous_generation_id=active.id,
        )

    route = service.get_route("global")
    assert route.active_generation_id == active.id
    assert route.route_version == 4


def test_compare_and_swap_route_updates_both_targets_and_version(db):
    service = IndexGenerationService(db)
    active = create_generation(service, "a")
    candidate = create_generation(service, "b")
    active.state = IndexGenerationState.ACTIVE.value
    candidate.state = IndexGenerationState.READY.value
    db.add(
        IndexGenerationRoute(
            scope="global",
            active_generation_id=active.id,
            route_version=4,
        )
    )
    db.commit()

    route = service.compare_and_swap_route(
        scope="global",
        expected_route_version=4,
        active_generation_id=candidate.id,
        previous_generation_id=active.id,
    )

    assert route.active_generation_id == candidate.id
    assert route.previous_generation_id == active.id
    assert route.route_version == 5
