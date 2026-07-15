"""Worker dependency readiness prevents task pulls while embeddings are unavailable."""

from types import SimpleNamespace

import pytest

from openrag.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingProviderError,
)
from openrag.worker import task_worker as worker_module
from openrag.worker.task_worker import TaskWorker


class FakeEngine:
    dimension = 3
    config = SimpleNamespace(probe_interval_seconds=30)

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def probe(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"ready": True}


def worker_with_engine(engine):
    worker = TaskWorker(worker_id="worker-test", poll_interval=1)
    worker.embedding_engine = engine
    worker.require_layer_vectors = False
    return worker


def test_configuration_error_exits_initialization(monkeypatch):
    def fail():
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding API key is not configured"
        )

    monkeypatch.setattr(worker_module, "EmbeddingEngine", fail)
    worker = TaskWorker(worker_id="worker-test")

    with pytest.raises(EmbeddingConfigurationError):
        worker._initialize_processing_dependencies()


def test_provider_failure_marks_unready_without_creating_store(monkeypatch):
    failure = EmbeddingProviderError(
        "EMBEDDING_PROVIDER_UNAVAILABLE",
        "Embedding service temporarily unavailable",
        retryable=True,
    )
    worker = worker_with_engine(FakeEngine([failure]))
    create_store = pytest.fail
    monkeypatch.setattr(worker_module, "_create_vector_store", create_store)

    assert worker._preflight_processing_dependencies() is False
    assert worker.dependencies_ready is False


def test_provider_recovery_allows_task_processing(monkeypatch):
    failure = EmbeddingProviderError(
        "EMBEDDING_PROVIDER_UNAVAILABLE",
        "Embedding service temporarily unavailable",
        retryable=True,
    )
    engine = FakeEngine([failure, True])
    worker = worker_with_engine(engine)
    store = SimpleNamespace(probe=lambda: None)
    monkeypatch.setattr(worker_module, "_create_vector_store", lambda _: store)

    assert worker._preflight_processing_dependencies() is False
    worker.next_dependency_probe_at = 0
    assert worker._ensure_processing_dependencies_ready() is True
    assert worker.vector_store is store


def test_unready_worker_does_not_pull_tasks(monkeypatch):
    worker = TaskWorker(worker_id="worker-test", poll_interval=0)
    monkeypatch.setattr(worker, "_initialize_processing_dependencies", lambda: None)
    monkeypatch.setattr(worker, "_ensure_processing_dependencies_ready", lambda: False)
    monkeypatch.setattr(worker, "_pull_one_task", lambda: pytest.fail("must not pull"))

    def stop_after_one_loop(_):
        worker.running = False

    monkeypatch.setattr(worker_module.time, "sleep", stop_after_one_loop)
    worker.start()


def test_l0_l1_disabled_does_not_require_layer_store(monkeypatch):
    worker = worker_with_engine(FakeEngine([True]))
    monkeypatch.setattr(
        worker_module,
        "_create_vector_store",
        lambda _: SimpleNamespace(probe=lambda: None),
    )
    monkeypatch.setattr(
        worker_module,
        "_create_layer_store",
        lambda *_: pytest.fail("layer store must not be initialized"),
    )

    assert worker._preflight_processing_dependencies() is True
