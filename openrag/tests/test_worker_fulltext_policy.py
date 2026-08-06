from types import SimpleNamespace

import pytest

from openrag.config import ElasticsearchConfig
from openrag.processors.document_processor import FulltextIndexingError
from openrag.retrieval import retrieval_service
from openrag.worker import task_worker


@pytest.mark.parametrize(
    ("config", "expected_required"),
    [
        (ElasticsearchConfig(enabled=False), False),
        (ElasticsearchConfig(enabled=True, hybrid_recall_mode="legacy"), True),
        (
            ElasticsearchConfig(
                enabled=True,
                hybrid_recall_mode="independent_rrf",
                chunk_index_mode="legacy",
            ),
            True,
        ),
    ],
)
def test_worker_initialization_uses_fulltext_requirement_policy(
    monkeypatch, caplog, config, expected_required
):
    captured = []
    store = object()
    monkeypatch.setattr(
        task_worker,
        "get_config",
        lambda: SimpleNamespace(elasticsearch=config),
    )
    monkeypatch.setattr(task_worker, "ParserRegistry", lambda: object())
    monkeypatch.setattr(task_worker, "ChunkEngine", lambda: object())
    monkeypatch.setattr(task_worker, "HierarchyStorage", lambda: object())
    monkeypatch.setattr(
        task_worker,
        "_create_es_chunk_store",
        lambda *, required: captured.append(required) or store,
    )
    monkeypatch.setattr(retrieval_service, "l0_l1_retrieval_enabled", lambda: False)
    caplog.set_level("INFO", logger=task_worker.__name__)

    worker = task_worker.TaskWorker()
    monkeypatch.setattr(worker, "_preflight_processing_dependencies", lambda: True)
    worker._initialize_processing_dependencies()

    assert captured == [expected_required]
    assert worker.fulltext_required is expected_required
    assert worker.chunk_fulltext_store is store
    assert f"fulltext_required={str(expected_required).lower()}" in caplog.text


def test_fulltext_failure_schedules_document_retry(monkeypatch, caplog):
    worker = task_worker.TaskWorker()
    pending_updates = []
    task_updates = []
    monkeypatch.setattr(
        worker,
        "_mark_file_pending_for_retry",
        lambda file_id, error: pending_updates.append((file_id, error)),
    )
    monkeypatch.setattr(
        worker,
        "_update_task_status",
        lambda task_id, status, **kwargs: task_updates.append(
            (task_id, status, kwargs)
        ),
    )
    caplog.set_level("WARNING", logger=task_worker.__name__)

    worker._handle_task_failure(
        {
            "id": 17,
            "task_type": "process_document",
            "file_id": 23,
            "retry_count": 0,
            "max_retries": 3,
        },
        FulltextIndexingError("partial_write:2/3"),
    )

    assert pending_updates == [
        (23, "FULLTEXT_INDEXING_FAILED: Elasticsearch chunk indexing failed")
    ]
    assert task_updates[0][0:2] == (17, "retry")
    assert task_updates[0][2]["error_code"] == "FULLTEXT_INDEXING_FAILED"
    assert task_updates[0][2]["error_retryable"] is True
    assert "document_processing_failed" in caplog.text
