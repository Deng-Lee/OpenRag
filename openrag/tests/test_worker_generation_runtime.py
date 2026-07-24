from types import SimpleNamespace

import pytest

from openrag.indexing.runtime import IndexRuntime
from openrag.worker.task_worker import TaskWorker


class Resolver:
    def __init__(self, runtime):
        self.runtime = runtime
        self.requested = []

    def get_generation_snapshot(self, _db, generation_id):
        self.requested.append(generation_id)
        return self.runtime.snapshot

    def get_runtime(self, snapshot):
        assert snapshot is self.runtime.snapshot
        return self.runtime


def _runtime(generation_id="generation-a", state="active"):
    snapshot = SimpleNamespace(
        generation_id=generation_id,
        embedding_fingerprint="a" * 64,
        chunk_collection_name=f"chunks_{generation_id}",
        layer_collection_name=f"layers_{generation_id}",
        state=state,
    )
    engine = SimpleNamespace(assert_fingerprint=lambda value: value == "a" * 64)
    vector = SimpleNamespace(collection_name=snapshot.chunk_collection_name)
    layer = SimpleNamespace(collection_name=snapshot.layer_collection_name)
    return IndexRuntime(snapshot, engine, vector, layer)


def test_worker_resolves_exact_task_generation_once(monkeypatch):
    runtime = _runtime("generation-a")
    resolver = Resolver(runtime)
    worker = TaskWorker(worker_id="worker-generation")
    worker.index_runtime_resolver = resolver
    monkeypatch.setattr("openrag.worker.task_worker.SessionLocal", lambda: object())

    resolved = worker._resolve_task_runtime(
        {
            "id": 1,
            "task_type": "process_document",
            "index_generation_id": "generation-a",
        }
    )

    assert resolved is runtime
    assert resolver.requested == ["generation-a"]


def test_worker_rejects_unbound_index_write_before_embedding():
    worker = TaskWorker(worker_id="worker-generation")

    with pytest.raises(RuntimeError, match="INDEX_GENERATION_UNBOUND"):
        worker._assert_task_generation_writable(
            {"id": 1, "task_type": "process_document"}
        )


def test_worker_rejects_runtime_store_mismatch():
    runtime = _runtime("generation-a")
    runtime.vector_store.collection_name = "chunks_generation-b"
    worker = TaskWorker(worker_id="worker-generation")

    with pytest.raises(RuntimeError, match="INDEX_RUNTIME_MISMATCH"):
        worker._assert_task_generation_writable(
            {
                "id": 1,
                "task_type": "process_document",
                "index_generation_id": "generation-a",
            },
            runtime,
        )


def test_worker_allows_mirror_only_against_retired_generation():
    worker = TaskWorker(worker_id="worker-generation")
    task = {
        "id": 2,
        "task_type": "mirror_previous_generation_file",
        "index_generation_id": "generation-previous",
    }
    worker._assert_task_generation_writable(
        task, _runtime("generation-previous", state="retired")
    )
    with pytest.raises(RuntimeError, match="mirror target is not previous"):
        worker._assert_task_generation_writable(
            task, _runtime("generation-previous", state="active")
        )


def test_worker_dispatches_reindex_without_online_document_processor(monkeypatch):
    worker = TaskWorker(worker_id="worker-generation")
    target = _runtime("generation-c", state="building")
    updates = []

    class Heartbeat:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def terminate(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr("openrag.worker.task_worker.multiprocessing.Process", Heartbeat)
    monkeypatch.setattr(worker, "_resolve_task_runtime", lambda _task: target)
    monkeypatch.setattr(
        worker,
        "_process_document",
        lambda *_args, **_kwargs: pytest.fail(
            "online DocumentProcessor must not run for candidate rebuild"
        ),
    )
    monkeypatch.setattr(
        worker,
        "_reindex_generation_file_task",
        lambda task, runtime: {
            "generation_id": task["index_generation_id"],
            "runtime": runtime.snapshot.generation_id,
        },
    )
    monkeypatch.setattr(
        worker,
        "_update_task_status",
        lambda task_id, status, **kwargs: updates.append((task_id, status, kwargs)),
    )

    worker._execute_task(
        {
            "id": 7,
            "task_type": "reindex_generation_file",
            "index_generation_id": "generation-c",
            "workspace_id": 1,
            "file_id": 2,
            "user_id": 3,
        }
    )

    assert [status for _, status, _ in updates] == ["started", "success"]
