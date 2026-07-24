"""Worker readiness probes the routed active generation without fixed stores."""

from types import SimpleNamespace

import pytest

from openrag.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingProviderError,
)
from openrag.worker import task_worker as worker_module
from openrag.worker.task_worker import TaskWorker


class Resolver:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def probe_active_runtime(self, _db):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def runtime(layer_store=None):
    return SimpleNamespace(
        embedding_engine=object(),
        vector_store=object(),
        layer_store=layer_store,
    )


def worker_with_resolver(monkeypatch, resolver):
    worker = TaskWorker(worker_id="worker-test", poll_interval=0)
    worker.index_runtime_resolver = resolver
    monkeypatch.setattr(
        worker_module, "SessionLocal", lambda: SimpleNamespace(close=lambda: None)
    )
    return worker


def test_configuration_error_exits_preflight(monkeypatch):
    failure = EmbeddingConfigurationError(
        "EMBEDDING_CONFIG_INVALID", "Embedding API key is not configured"
    )
    worker = worker_with_resolver(monkeypatch, Resolver([failure]))

    with pytest.raises(EmbeddingConfigurationError):
        worker._preflight_processing_dependencies()


def test_provider_failure_marks_active_route_unready(monkeypatch):
    failure = EmbeddingProviderError(
        "EMBEDDING_PROVIDER_UNAVAILABLE",
        "Embedding service temporarily unavailable",
        retryable=True,
    )
    worker = worker_with_resolver(monkeypatch, Resolver([failure]))

    assert worker._preflight_processing_dependencies() is False
    assert worker.dependencies_ready is False


def test_provider_recovery_makes_active_route_ready(monkeypatch):
    failure = EmbeddingProviderError(
        "EMBEDDING_PROVIDER_UNAVAILABLE",
        "Embedding service temporarily unavailable",
        retryable=True,
    )
    expected = runtime()
    worker = worker_with_resolver(monkeypatch, Resolver([failure, expected]))

    assert worker._preflight_processing_dependencies() is False
    worker.next_dependency_probe_at = 0
    assert worker._ensure_processing_dependencies_ready() is True
    assert worker.vector_store is expected.vector_store


def test_unready_active_runtime_does_not_block_delete_task_pull(monkeypatch):
    worker = TaskWorker(worker_id="worker-test", poll_interval=0)
    monkeypatch.setattr(worker, "_initialize_processing_dependencies", lambda: None)
    monkeypatch.setattr(worker, "_ensure_processing_dependencies_ready", lambda: False)
    pulled = []

    def pull_once():
        pulled.append(True)
        worker.running = False
        return None

    monkeypatch.setattr(worker, "_pull_one_task", pull_once)
    monkeypatch.setattr(worker_module.time, "sleep", lambda _: None)

    worker.start()

    assert pulled == [True]


def test_runtime_without_layers_is_valid_when_route_disables_them(monkeypatch):
    worker = worker_with_resolver(monkeypatch, Resolver([runtime(layer_store=None)]))

    assert worker._preflight_processing_dependencies() is True
    assert worker.layer_store is None
