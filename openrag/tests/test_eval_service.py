import json
from pathlib import Path

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


def test_a01_fixture_imports_and_independent_mode_improves_sparse_cases(db_session):
    items = json.loads(
        (Path(__file__).parent / "fixtures" / "a01_retrieval_eval.json").read_text(
            encoding="utf-8"
        )
    )
    answers = {
        item["query_text"]: [
            {
                "chunk_id": judgment["chunk_id"],
                "file_id": judgment["file_id"],
                "score": 1.0,
            }
            for judgment in item["judgments"]
        ]
        for item in items
    }
    sparse_queries = {
        item["query_text"]
        for item in items
        if item.get("metadata", {}).get("expected_channel") == "sparse"
    }

    def legacy_search(query, **kwargs):
        return [] if query in sparse_queries else answers[query]

    def independent_search(query, **kwargs):
        return answers[query]

    service = EvalService(db_session, search_callable=legacy_search)
    dataset = service.create_dataset(name="a01", workspace_id=10)
    imported = service.import_dataset(dataset.id, items)
    assert imported == {"query_count": 6, "judgment_count": 5}
    legacy_run = service.create_eval_run(
        dataset.id, {"top_k": 10, "hybrid_recall_mode": "legacy"}
    )
    service.execute_eval_run(legacy_run.id)

    service.search_callable = independent_search
    independent_run = service.create_eval_run(
        dataset.id, {"top_k": 10, "hybrid_recall_mode": "independent_rrf"}
    )
    service.execute_eval_run(independent_run.id)

    summaries = {
        result.eval_run_id: result.metrics
        for result in db_session.query(EvalResult)
        .filter(EvalResult.metric_scope == "run_summary")
        .all()
    }
    assert summaries[independent_run.id]["recall@10"] > summaries[legacy_run.id]["recall@10"]
    assert summaries[independent_run.id]["mrr@50"] > summaries[legacy_run.id]["mrr@50"]


def test_a02_fixture_freezes_v1_content_and_exact_identifier_baseline(db_session):
    items = json.loads(
        (Path(__file__).parent / "fixtures" / "a02_content_exact_eval.json").read_text(
            encoding="utf-8"
        )
    )
    answers = {
        item["query_text"]: [
            {
                "chunk_id": judgment["chunk_id"],
                "file_id": judgment["file_id"],
                "score": 1.0,
            }
            for judgment in item["judgments"]
        ]
        for item in items
    }
    exact_queries = {
        item["query_text"]
        for item in items
        if item["query_type"] == "exact_identifier"
    }

    def legacy_content_search(query, **kwargs):
        if query in exact_queries:
            distractors = [
                {"chunk_id": f"legacy-distractor-{index}", "file_id": 900 + index, "score": 1.0}
                for index in range(10)
            ]
            return distractors + answers[query]
        return answers[query]

    service = EvalService(db_session, search_callable=legacy_content_search)
    dataset = service.create_dataset(
        name="a02-content-exact-v1",
        workspace_id=10,
        metadata={"fixture": "a02_content_exact_eval.json"},
    )
    assert service.import_dataset(dataset.id, items) == {
        "query_count": 8,
        "judgment_count": 7,
    }

    run = service.create_eval_run(
        dataset.id,
        {
            "top_k": 20,
            "chunk_index_mode": "legacy",
            "query_profile": "legacy-content",
        },
        index_version="v1",
    )
    service.execute_eval_run(run.id)

    summary = (
        db_session.query(EvalResult)
        .filter(EvalResult.eval_run_id == run.id, EvalResult.metric_scope == "run_summary")
        .one()
    )
    assert summary.metrics["query_count"] == 8
    assert summary.metrics["failed_count"] == 0
    assert summary.metrics["recall@20"] == pytest.approx(7 / 8)
    assert summary.metrics["mrr@50"] < 1.0
    assert summary.metrics["mrr@10"] < summary.metrics["mrr@50"]
    assert summary.metrics["p95_latency_ms"] >= 0
    groups = summary.metrics["query_type_groups"]
    assert groups["content_natural_language"]["recall@20"] == 1.0
    assert groups["exact_identifier"]["query_count"] == 5
    assert groups["exact_identifier"]["hit_rate@10"] == 0.0
    assert groups["no_answer"]["false_positive_rate"] == 0.0
    assert summary.metrics["exact_identifier_hit_rate@10"] == 0.0
    assert summary.metrics["no_answer_false_positive_rate"] == 0.0


def test_a02_grouped_summary_counts_no_answer_false_positives(db_session):
    service = EvalService(
        db_session,
        search_callable=lambda query, **kwargs: [
            {"chunk_id": "distractor", "file_id": 999, "score": 1.0}
        ],
    )
    dataset = service.create_dataset(name="a02-no-answer", workspace_id=10)
    service.add_query(
        dataset.id,
        "NO-ANSWER-A02-999",
        query_type="no_answer",
    )
    run = service.create_eval_run(dataset.id, {"top_k": 20}, index_version="v2")

    service.execute_eval_run(run.id)

    summary = (
        db_session.query(EvalResult)
        .filter(
            EvalResult.eval_run_id == run.id,
            EvalResult.metric_scope == "run_summary",
        )
        .one()
    )
    assert summary.metrics["no_answer_false_positive_rate"] == 1.0
    assert (
        summary.metrics["query_type_groups"]["no_answer"]["false_positive_rate"]
        == 1.0
    )
