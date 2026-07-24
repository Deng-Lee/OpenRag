from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.activation_service import (
    ActivationConflictError,
    IndexActivationError,
    IndexActivationService,
)
from openrag.indexing.alias_reconciler import AliasReconciler
from openrag.models import Base
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationRoute,
    IndexGenerationState,
)


class FakeAliasBackend:
    def __init__(self, fail_alias=None):
        self.targets = {}
        self.fail_alias = fail_alias

    def target(self, alias):
        return self.targets.get(alias)

    def set_alias(self, alias, collection):
        if alias == self.fail_alias:
            raise RuntimeError("alias failure")
        self.targets[alias] = collection


def _generation(generation_id, state, source=None):
    return IndexGeneration(
        id=generation_id,
        scope="global",
        state=state.value,
        source_generation_id=source,
        embedding_provider="provider",
        embedding_model="model",
        embedding_revision="revision",
        embedding_dimension=3,
        embedding_fingerprint=generation_id[0] * 64,
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=2,
        chunk_policy_revision="chunk-v2",
        hierarchy_policy_revision="hierarchy-v2",
        chunk_collection_name=f"chunks_{generation_id[0]}",
        layer_collection_name=f"layers_{generation_id[0]}",
        manifest={"rollback_window_seconds": 3600},
        validation_report={"passed": True},
        quality_report={"passed": True},
        quality_gate_passed=True,
        ready_at=datetime.now(timezone.utc),
    )


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    active = _generation(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", IndexGenerationState.ACTIVE
    )
    candidate = _generation(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        IndexGenerationState.READY,
        source=active.id,
    )
    route = IndexGenerationRoute(
        scope="global", active_generation_id=active.id, route_version=4
    )
    db.add_all([active, candidate, route])
    db.commit()
    try:
        yield db, active, candidate, route
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def _service(db, backend, *, lag=0, validation=True, smoke=True):
    return IndexActivationService(
        db,
        AliasReconciler(backend),
        final_reconcile=lambda _generation_id: lag,
        quick_validate=lambda _generation_id: validation,
        smoke_test=lambda _generation_id: smoke,
        drain_timeout_seconds=0.01,
    )


def test_candidate_becomes_active_as_one_route_pair(env):
    db, active, candidate, route = env
    backend = FakeAliasBackend()
    result = _service(db, backend).activate_generation(
        candidate.id, expected_route_version=4, activated_by=7
    )

    db.refresh(route)
    db.refresh(active)
    db.refresh(candidate)
    assert route.active_generation_id == candidate.id
    assert route.previous_generation_id == active.id
    assert route.route_version == 5
    assert route.write_barrier is False
    assert active.state == IndexGenerationState.RETIRED.value
    assert candidate.state == IndexGenerationState.ACTIVE.value
    assert result["alias_sync_result"]["passed"] is True
    assert backend.targets == {
        "openrag_chunks_active": candidate.chunk_collection_name,
        "openrag_layers_active": candidate.layer_collection_name,
    }


def test_stale_or_second_activation_is_rejected_without_route_change(env):
    db, active, candidate, route = env
    service = _service(db, FakeAliasBackend())
    service.activate_generation(candidate.id, expected_route_version=4, activated_by=7)

    with pytest.raises(ActivationConflictError):
        service.activate_generation(candidate.id, expected_route_version=4, activated_by=8)
    db.refresh(route)
    assert route.active_generation_id == candidate.id
    assert route.route_version == 5
    assert route.write_barrier is False


def test_nonzero_final_lag_keeps_old_route_and_releases_barrier(env):
    db, active, candidate, route = env
    with pytest.raises(IndexActivationError):
        _service(db, FakeAliasBackend(), lag=1).activate_generation(
            candidate.id, expected_route_version=4, activated_by=7
        )

    db.refresh(route)
    assert route.active_generation_id == active.id
    assert route.route_version == 4
    assert route.write_barrier is False


def test_second_alias_failure_does_not_revert_postgres_route(env):
    db, active, candidate, route = env
    backend = FakeAliasBackend(fail_alias="openrag_layers_active")
    result = _service(db, backend).activate_generation(
        candidate.id, expected_route_version=4, activated_by=7
    )

    db.refresh(route)
    assert route.active_generation_id == candidate.id
    assert result["alias_sync_result"]["passed"] is False
    assert result["alias_sync_result"]["failed_aliases"] == [
        "openrag_layers_active"
    ]
    assert backend.targets["openrag_chunks_active"] == candidate.chunk_collection_name
