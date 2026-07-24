from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.alias_reconciler import AliasReconciler
from openrag.indexing.rollback_service import IndexRollbackError, IndexRollbackService
from openrag.models import Base
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationRoute,
    IndexGenerationState,
)


class AliasBackend:
    def __init__(self):
        self.targets = {}

    def target(self, alias):
        return self.targets.get(alias)

    def set_alias(self, alias, collection):
        self.targets[alias] = collection


def _generation(generation_id, state):
    return IndexGeneration(
        id=generation_id,
        scope="global",
        state=state.value,
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
    )


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    current = _generation(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", IndexGenerationState.ACTIVE
    )
    previous = _generation(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", IndexGenerationState.RETIRED
    )
    route = IndexGenerationRoute(
        scope="global",
        active_generation_id=current.id,
        previous_generation_id=previous.id,
        route_version=8,
        rollback_deadline=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add_all([current, previous, route])
    db.commit()
    try:
        yield db, current, previous, route
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def _service(db, *, lag=0, available=True):
    return IndexRollbackService(
        db,
        AliasReconciler(AliasBackend()),
        previous_lag=lambda: lag,
        previous_available=lambda _id: available,
        smoke_test=lambda _id: True,
    )


def test_zero_lag_rollback_switches_and_keeps_new_generation_retired(env):
    db, current, previous, route = env
    result = _service(db).rollback_to_previous(
        expected_route_version=8, rolled_back_by=7
    )

    db.refresh(route)
    db.refresh(current)
    db.refresh(previous)
    assert route.active_generation_id == previous.id
    assert route.previous_generation_id == current.id
    assert route.route_version == 9
    assert previous.state == IndexGenerationState.ACTIVE.value
    assert current.state == IndexGenerationState.RETIRED.value
    assert result["accepted_rpo_lag_files"] == 0


def test_nonzero_lag_or_unavailable_provider_keeps_current_active(env):
    db, current, previous, route = env
    with pytest.raises(IndexRollbackError):
        _service(db, lag=2).rollback_to_previous(
            expected_route_version=8, rolled_back_by=7
        )
    with pytest.raises(IndexRollbackError):
        _service(db, available=False).rollback_to_previous(
            expected_route_version=8, rolled_back_by=7
        )

    db.refresh(route)
    assert route.active_generation_id == current.id
    assert route.route_version == 8


def test_explicit_rpo_acceptance_and_consecutive_rollback_increase_version(env):
    db, current, previous, route = env
    first = _service(db, lag=3).rollback_to_previous(
        expected_route_version=8, rolled_back_by=7, accept_rpo=True
    )
    second = _service(db, lag=0).rollback_to_previous(
        expected_route_version=9, rolled_back_by=7
    )

    db.refresh(route)
    assert first["accepted_rpo_lag_files"] == 3
    assert second["new_route_version"] == 10
    assert route.active_generation_id == current.id
