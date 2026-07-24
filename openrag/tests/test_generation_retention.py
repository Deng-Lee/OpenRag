from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.backup_service import BackupRequiredError, BackupService
from openrag.indexing.milvus_cleaner import CleanupGuardError, MilvusCollectionCleaner
from openrag.indexing.retention_service import RetentionService
from openrag.models import Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute, IndexGenerationState


class Backend:
    def __init__(self):
        self.aliases = {}
        self.collections = {"chunks_c", "layers_c"}
        self.released = []
        self.dropped = []

    def aliases_for(self, name):
        return tuple(self.aliases.get(name, ()))

    def release_collection(self, name):
        self.released.append(name)

    def drop_collection(self, name):
        self.dropped.append(name)
        self.collections.discard(name)

    def has_collection(self, name):
        return name in self.collections


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
        delete_after=datetime.now(timezone.utc) - timedelta(minutes=1),
    )


def test_dry_plan_has_no_writes_and_route_alias_retention_guards_fail_closed():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    active = _generation("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", IndexGenerationState.ACTIVE)
    previous = _generation("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", IndexGenerationState.RETIRED)
    candidate = _generation("cccccccc-cccc-cccc-cccc-cccccccccccc", IndexGenerationState.RETIRED)
    db.add_all([active, previous, candidate])
    db.flush()
    db.add(IndexGenerationRoute(scope="global", active_generation_id=active.id, previous_generation_id=previous.id))
    db.commit()
    backend = Backend()
    cleaner = MilvusCollectionCleaner(backend)
    service = RetentionService(db, cleaner, BackupService(None, threshold_entities=100))

    plan = service.build_delete_plan(candidate.id)
    assert plan["collections"] == ["chunks_c", "layers_c"]
    assert backend.released == backend.dropped == []
    with pytest.raises(CleanupGuardError):
        service.assert_deletable(active)
    with pytest.raises(CleanupGuardError):
        service.assert_deletable(previous)
    backend.aliases["chunks_c"] = ["openrag_chunks_active"]
    with pytest.raises(CleanupGuardError):
        service.assert_deletable(candidate)

    backend.aliases.clear()
    candidate.delete_after = datetime.now(timezone.utc) + timedelta(hours=1)
    db.commit()
    with pytest.raises(CleanupGuardError):
        service.assert_deletable(candidate)
    db.close()
    Base.metadata.drop_all(engine)


def test_required_backup_blocks_delete_and_deleted_audit_row_is_retained():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    generation = _generation("cccccccc-cccc-cccc-cccc-cccccccccccc", IndexGenerationState.RETIRED)
    generation.indexed_chunk_count = 10
    db.add(generation)
    db.commit()
    backend = Backend()
    cleaner = MilvusCollectionCleaner(backend)
    service = RetentionService(db, cleaner, BackupService(None, threshold_entities=1))
    with pytest.raises(BackupRequiredError):
        service.assert_deletable(generation)

    generation.indexed_chunk_count = 0
    db.commit()
    plan = service.build_delete_plan(generation.id)
    service.mark_deleting(generation.id, deleted_by=7, plan=plan)
    cleaner.drop_generation_collections(generation, active_id=None, previous_id=None, confirmation=generation.id)
    service.mark_deleted(generation.id)
    db.refresh(generation)
    assert generation.state == IndexGenerationState.DELETED.value
    assert generation.deleted_at is not None
    assert generation.deletion_plan["generation_id"] == generation.id
    assert db.get(IndexGeneration, generation.id) is generation
    db.close()
    Base.metadata.drop_all(engine)
