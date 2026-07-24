from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.reindex_service import GenerationReindexService
from openrag.indexing.source_reader import GenerationSourceSnapshot
from openrag.models import Base
from openrag.models.file import File, ProcessingStatus
from openrag.models.index_generation import (
    IndexGenerationFile,
    IndexGenerationFileState,
)
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.index_generation_service import IndexGenerationService
from openrag.services.index_generation_service import GenerationStateTransitionError


def _generation_values():
    return {
        "scope": "global",
        "embedding_provider": "provider",
        "embedding_model": "model",
        "embedding_revision": "revision",
        "embedding_dimension": 3,
        "embedding_fingerprint": "b" * 64,
        "embedding_config_ref": "embedding/candidate",
        "vector_normalization": "none",
        "distance_metric": "COSINE",
        "schema_version": 2,
        "chunk_policy_revision": "chunk-v2",
        "hierarchy_policy_revision": "hierarchy-v2",
        "chunk_collection_name": "chunks_candidate",
        "layer_collection_name": "layers_candidate",
        "manifest": {},
    }


def test_initialize_enqueue_limit_and_recover_interrupted_file():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="reindex-user",
        email="reindex@example.com",
        password_hash="hash",
        full_name="Reindex User",
        is_active=True,
    )
    db.add(user)
    db.flush()
    workspace = Workspace(name="Reindex", slug="reindex", owner_id=user.id)
    db.add(workspace)
    db.flush()
    files = []
    for index in range(3):
        file = File(
            uri=f"docs/{index}.txt",
            name=f"{index}.txt",
            owner_id=user.id,
            workspace_id=workspace.id,
            is_directory=False,
            size=1,
            processing_status=ProcessingStatus.completed,
        )
        db.add(file)
        files.append(file)
    db.flush()
    generation = IndexGenerationService(db).create_generation(**_generation_values())
    generation.state = "building"
    db.commit()

    class Reader:
        def list_active_source_files(self):
            return files

        def get_source_revision(self, file_id):
            file = next(item for item in files if item.id == file_id)
            return GenerationSourceSnapshot(
                file_id=file.id,
                workspace_id=file.workspace_id,
                source_updated_at=file.updated_at,
                source_content_hash=str(file.id) * 64,
                chunks=(SimpleNamespace(), SimpleNamespace()),
                layers=(("l0", "summary"),),
            )

    service = GenerationReindexService(db, Reader())
    assert service.initialize_generation_files(generation.id) == 3
    tasks = service.enqueue_pending_files(
        generation.id, user_id=user.id, limit=10, max_concurrent=2
    )
    assert len(tasks) == 2
    assert {task.queue for task in tasks} == {"reindex"}
    assert all(task.index_generation_id == generation.id for task in tasks)

    service.pause_generation(generation.id)
    assert generation.build_paused is True
    assert service.enqueue_pending_files(generation.id, user_id=user.id) == []
    service.resume_generation(generation.id)
    resumed_tasks = service.enqueue_pending_files(
        generation.id, user_id=user.id, limit=10, max_concurrent=2
    )
    assert len(resumed_tasks) == 2

    claimed = service.claim_file(
        generation.id, resumed_tasks[0].file_id, worker_id="worker-dead"
    )
    claimed.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    db.commit()
    assert service.recover_stale_files(generation.id) == 1
    assert (
        db.get(IndexGenerationFile, (generation.id, resumed_tasks[0].file_id)).state
        == IndexGenerationFileState.STALE.value
    )
    reclaimed = service.claim_file(
        generation.id, resumed_tasks[0].file_id, worker_id="worker-new"
    )
    assert reclaimed.worker_id == "worker-new"
    with pytest.raises(GenerationStateTransitionError):
        service.cancel_generation(generation.id)

    db.close()
    Base.metadata.drop_all(engine)
