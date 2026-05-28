"""Tests for trace context propagation across API and worker entry points."""

import asyncio

from fastapi.testclient import TestClient

from openrag.tracing.context import (
    get_trace_context,
    pop_span,
    push_span,
    reset_trace_context,
    set_trace_context,
)


def teardown_function():
    reset_trace_context()


def test_trace_context_sets_resets_and_nests_spans():
    set_trace_context(
        trace_id="trace-1",
        trace_type="retrieval",
        workspace_id=10,
        user_id=20,
        file_id=30,
        task_id="task-1",
        eval_run_id=40,
        eval_query_id=50,
        sampling_reason="unit-test",
    )

    ctx = get_trace_context()
    assert ctx == {
        "trace_id": "trace-1",
        "span_id": None,
        "trace_type": "retrieval",
        "workspace_id": 10,
        "user_id": 20,
        "file_id": 30,
        "task_id": "task-1",
        "eval_run_id": 40,
        "eval_query_id": 50,
        "sampling_reason": "unit-test",
    }

    assert push_span("span-parent") == "span-parent"
    assert get_trace_context()["span_id"] == "span-parent"
    assert push_span("span-child") == "span-child"
    assert get_trace_context()["span_id"] == "span-child"
    assert pop_span() == "span-child"
    assert get_trace_context()["span_id"] == "span-parent"
    assert pop_span() == "span-parent"
    assert get_trace_context()["span_id"] is None

    reset_trace_context()
    assert all(value is None for value in get_trace_context().values())


def test_trace_context_is_isolated_between_concurrent_tasks():
    async def run_with_trace(trace_id: str):
        set_trace_context(trace_id=trace_id, trace_type="retrieval")
        await asyncio.sleep(0)
        observed = get_trace_context()
        reset_trace_context()
        return observed

    async def run_both():
        return await asyncio.gather(
            run_with_trace("trace-a"),
            run_with_trace("trace-b"),
        )

    first, second = asyncio.run(run_both())

    assert first["trace_id"] == "trace-a"
    assert second["trace_id"] == "trace-b"
    assert first["trace_type"] == second["trace_type"] == "retrieval"


def test_api_middleware_sets_response_header_and_clears_context():
    from openrag.api.main import app

    @app.get("/search/_trace-context-test", include_in_schema=False)
    async def _trace_context_test():
        return get_trace_context()

    client = TestClient(app)
    response = client.get(
        "/search/_trace-context-test",
        headers={"X-OpenRag-Trace-Id": "trace-from-client"},
    )

    assert response.status_code == 200
    assert response.headers["X-OpenRag-Trace-Id"] == "trace-from-client"
    assert response.json()["trace_id"] == "trace-from-client"
    assert response.json()["trace_type"] == "retrieval"
    assert get_trace_context()["trace_id"] is None


def test_task_worker_sets_document_processing_context(monkeypatch):
    from openrag.worker import task_worker
    from openrag.worker.task_worker import TaskWorker

    observed = {}

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def terminate(self):
            pass

        def join(self, timeout=None):
            pass

    def fake_process_document(self, task, task_id):
        observed.update(get_trace_context())
        return {"status": "success"}

    monkeypatch.setattr(task_worker.multiprocessing, "Process", FakeProcess)
    monkeypatch.setattr(TaskWorker, "_update_task_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(TaskWorker, "_process_document", fake_process_document)

    worker = TaskWorker(api_base_url="http://testserver")
    worker._execute_task(
        {
            "id": "task-42",
            "task_type": "process_document",
            "file_id": 101,
            "workspace_id": 202,
            "user_id": 303,
        }
    )

    assert observed["trace_type"] == "document_processing"
    assert observed["task_id"] == "task-42"
    assert observed["file_id"] == 101
    assert observed["workspace_id"] == 202
    assert observed["user_id"] == 303
    assert observed["trace_id"]
    assert get_trace_context()["trace_id"] is None
