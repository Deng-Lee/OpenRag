from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.health import build_index_health_snapshot
from openrag.models import Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute, IndexGenerationState


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
        manifest={},
    )


def test_health_distinguishes_active_readiness_candidate_failure_and_alias_drift():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    active = _generation("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", IndexGenerationState.ACTIVE)
    previous = _generation("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", IndexGenerationState.RETIRED)
    previous.mirror_lag_files = 2
    failed = _generation("cccccccc-cccc-cccc-cccc-cccccccccccc", IndexGenerationState.FAILED)
    db.add_all([active, previous, failed])
    db.flush()
    db.add(IndexGenerationRoute(scope="global", active_generation_id=active.id, previous_generation_id=previous.id, route_version=6))
    db.commit()

    missing = build_index_health_snapshot(
        db,
        runtime_probe=lambda _id: {"chunks": False, "layers": False},
        alias_snapshot=lambda: {},
    )
    assert missing["ready"] is False
    assert missing["active_generation_id"] == active.id
    assert missing["candidate_states"][failed.id] == "failed"
    assert missing["previous_lag_files"] == 2
    assert missing["alias_consistent"] is False
    assert all("password" not in key and "api_key" not in key for key in missing)

    ready = build_index_health_snapshot(
        db,
        runtime_probe=lambda _id: {"chunks": True, "layers": True},
    )
    assert ready["ready"] is True
    db.close()
    Base.metadata.drop_all(engine)
