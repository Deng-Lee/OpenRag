from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationState
from openrag.services import file_deletion


def _generation(generation_id, state, created_at):
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
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            _generation(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                IndexGenerationState.RETIRED,
                now,
            ),
            _generation(
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                IndexGenerationState.ACTIVE,
                now + timedelta(seconds=1),
            ),
            _generation(
                "cccccccc-cccc-cccc-cccc-cccccccccccc",
                IndexGenerationState.BUILDING,
                now + timedelta(seconds=2),
            ),
        ]
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def test_retired_failure_does_not_block_active_or_candidate_delete(db, monkeypatch):
    calls = []

    def delete_one(_db, generation, file_id, **_kwargs):
        calls.append((generation.id, file_id))
        if generation.state == IndexGenerationState.RETIRED.value:
            raise OSError("retired collection unavailable")

    monkeypatch.setattr(file_deletion, "delete_file_from_generation", delete_one)
    monkeypatch.setattr(
        file_deletion,
        "_delete_elasticsearch_chunks_for_file_best_effort",
        lambda _file_id: True,
    )

    with pytest.raises(file_deletion.GenerationDeletePropagationError) as exc_info:
        file_deletion.delete_vectors_for_file_across_generations(db, 41)

    result = exc_info.value.result
    assert result["target_generation_ids"] == [item[0] for item in calls]
    assert result["failed_generation_ids"] == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ]
    assert result["succeeded_generation_ids"] == [
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
    ]
    assert {file_id for _, file_id in calls} == {41}


def test_failed_generation_is_targeted_only_after_build_started(db):
    now = datetime.now(timezone.utc)
    failed_empty = _generation(
        "dddddddd-dddd-dddd-dddd-dddddddddddd",
        IndexGenerationState.FAILED,
        now + timedelta(seconds=3),
    )
    failed_partial = _generation(
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        IndexGenerationState.FAILED,
        now + timedelta(seconds=4),
    )
    failed_partial.build_started_at = now
    db.add_all([failed_empty, failed_partial])
    db.commit()

    target_ids = {
        generation.id
        for generation in file_deletion.resolve_generations_containing_file(db, 41)
    }

    assert failed_empty.id not in target_ids
    assert failed_partial.id in target_ids


def test_repeated_delete_is_idempotent_and_scoped_to_file_id(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        file_deletion,
        "delete_file_from_generation",
        lambda _db, generation, file_id, **_kwargs: calls.append(
            (generation.id, file_id)
        ),
    )
    monkeypatch.setattr(
        file_deletion,
        "_delete_elasticsearch_chunks_for_file_best_effort",
        lambda _file_id: True,
    )

    first = file_deletion.delete_vectors_for_file_across_generations(db, 52)
    second = file_deletion.delete_vectors_for_file_across_generations(db, 52)

    assert first["failed_generation_ids"] == []
    assert second["failed_generation_ids"] == []
    assert len(calls) == 6
    assert {file_id for _, file_id in calls} == {52}


def test_worker_persists_partial_generation_result_when_retrying(monkeypatch):
    from openrag.worker.task_worker import TaskWorker

    result = {
        "file_id": 63,
        "target_generation_ids": ["active", "retired"],
        "succeeded_generation_ids": ["active"],
        "failed_generation_ids": ["retired"],
        "failed_subsystems": [],
    }
    error = file_deletion.GenerationDeletePropagationError(result)
    updates = []
    worker = TaskWorker.__new__(TaskWorker)
    monkeypatch.setattr(
        worker,
        "_update_task_status",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    worker._handle_task_failure(
        {
            "id": 9,
            "task_type": "purge_file_from_generations",
            "file_id": 63,
            "retry_count": 0,
            "max_retries": 3,
        },
        error,
    )

    assert updates[0][0][:2] == (9, "retry")
    assert updates[0][1]["error_code"] == "GENERATION_DELETE_INCOMPLETE"
    assert updates[0][1]["result"] == result


def test_worker_retries_file_storage_cleanup_failure(monkeypatch):
    from openrag.worker.task_worker import TaskWorker

    updates = []
    worker = TaskWorker.__new__(TaskWorker)
    monkeypatch.setattr(
        worker,
        "_update_task_status",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )

    worker._handle_task_failure(
        {
            "id": 10,
            "task_type": "delete_file",
            "file_id": 64,
            "retry_count": 0,
            "max_retries": 3,
        },
        file_deletion.FileStorageCleanupError(64),
    )

    assert updates[0][0][:2] == (10, "retry")
    assert updates[0][1]["error_code"] == "FILE_STORAGE_CLEANUP_FAILED"
    assert updates[0][1]["result"] == {
        "file_id": 64,
        "subsystem": "object_storage",
    }
