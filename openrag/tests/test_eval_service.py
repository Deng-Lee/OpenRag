import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401 - register all models
from openrag.models import EvalResult, EvalRun, TraceRun, TraceSnapshot
from openrag.models.base import Base
from openrag.services.eval_service import EvalService


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def test_eval_run_executes_three_query_dataset_and_writes_query_and_run_results(db_session):
    calls = []

    def fake_search(query, **kwargs):
        calls.append((query, kwargs))
        results_by_query = {
            "alpha": [{"chunk_id": "a", "file_id": 1, "score": 0.9}],
            "beta": [{"chunk_id": "x", "file_id": 2, "score": 0.8}],
            "gamma": [{"chunk_id": "g", "file_id": 3, "score": 0.7}],
        }
        return {
            "results": results_by_query[query],
            "stage_snapshots": {
                "vector": results_by_query[query],
                "rerank": list(reversed(results_by_query[query])),
            },
        }

    service = EvalService(db_session, search_callable=fake_search)
    dataset = service.create_dataset(
        name="small",
        workspace_id=10,
        description="three query dataset",
        metadata={"suite": "unit"},
    )
    service.import_dataset(
        dataset.id,
        [
            {
                "query_text": "alpha",
                "judgments": [{"chunk_id": "a", "file_id": 1, "relevance_grade": 3}],
            },
            {
                "query_text": "beta",
                "judgments": [{"chunk_id": "b", "file_id": 2, "relevance_grade": 3}],
            },
            {
                "query_text": "gamma",
                "judgments": [{"chunk_id": "g", "file_id": 3, "relevance_grade": 2}],
            },
        ],
    )

    assert service.get_dataset(dataset.id).name == "small"
    assert [item.id for item in service.list_datasets(workspace_id=10)] == [dataset.id]

    run = service.create_eval_run(
        dataset.id,
        search_config_snapshot={"top_k": 5, "use_rerank": True},
        created_by=99,
    )
    finished = service.execute_eval_run(run.id)

    assert finished.status == "success"
    assert len(calls) == 3
    assert all(call_kwargs["workspace_id"] == 10 for _, call_kwargs in calls)
    assert all(call_kwargs["top_k"] == 5 for _, call_kwargs in calls)

    query_results = (
        db_session.query(EvalResult)
        .filter(EvalResult.eval_run_id == run.id, EvalResult.metric_scope == "query")
        .order_by(EvalResult.eval_query_id)
        .all()
    )
    assert len(query_results) == 3
    assert [result.metrics["status"] for result in query_results] == [
        "success",
        "success",
        "success",
    ]
    assert query_results[0].metrics["precision@5"] == pytest.approx(1 / 5)
    assert query_results[1].metrics["hit_rate@5"] == 0.0

    summary = (
        db_session.query(EvalResult)
        .filter(EvalResult.eval_run_id == run.id, EvalResult.metric_scope == "run_summary")
        .one()
    )
    assert summary.eval_query_id is None
    assert summary.metrics["query_count"] == 3
    assert summary.metrics["failed_count"] == 0
    assert summary.metrics["success_count"] == 3
    assert summary.metrics["precision@5"] == pytest.approx((1 / 5 + 0.0 + 1 / 5) / 3)

    traces = db_session.query(TraceRun).filter(TraceRun.eval_run_id == run.id).all()
    assert len(traces) == 3
    assert {trace.trace_type for trace in traces} == {"eval_retrieval"}
    assert {trace.status for trace in traces} == {"success"}
    assert db_session.query(TraceSnapshot).count() == 6


def test_eval_run_records_failed_query_and_excludes_it_from_summary(db_session):
    def fake_search(query, **kwargs):
        if query == "broken":
            raise RuntimeError("search unavailable")
        return [{"chunk_id": "ok", "file_id": 1, "score": 1.0}]

    service = EvalService(db_session, search_callable=fake_search)
    dataset = service.create_dataset(name="failure", workspace_id=10)
    ok_query = service.add_query(dataset.id, "ok")
    service.add_judgment(dataset.id, ok_query.id, chunk_id="ok", relevance_grade=3)
    broken_query = service.add_query(dataset.id, "broken")
    service.add_judgment(dataset.id, broken_query.id, chunk_id="broken", relevance_grade=3)
    run = service.create_eval_run(dataset.id, {"top_k": 5})

    finished = service.execute_eval_run(run.id)

    assert finished.status == "success"
    failed_result = (
        db_session.query(EvalResult)
        .filter(EvalResult.eval_run_id == run.id, EvalResult.eval_query_id == broken_query.id)
        .one()
    )
    assert failed_result.metrics["status"] == "failed"
    assert "search unavailable" in failed_result.metrics["error"]

    summary = (
        db_session.query(EvalResult)
        .filter(EvalResult.eval_run_id == run.id, EvalResult.metric_scope == "run_summary")
        .one()
    )
    assert summary.metrics["query_count"] == 2
    assert summary.metrics["failed_count"] == 1
    assert summary.metrics["success_count"] == 1
    assert summary.metrics["precision@5"] == pytest.approx(1 / 5)

    traces = db_session.query(TraceRun).filter(TraceRun.eval_run_id == run.id).all()
    assert {trace.status for trace in traces} == {"success", "failed"}
