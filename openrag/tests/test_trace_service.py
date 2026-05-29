"""Tests for best-effort trace persistence service."""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401 - register all models
from openrag.models import TraceArtifact, TraceRun, TraceSnapshot, TraceSpan
from openrag.models.base import Base
from openrag.services.trace_service import TraceService
from openrag.tracing.context import get_trace_context, reset_trace_context, set_trace_context


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


def teardown_function():
    reset_trace_context()


def test_trace_service_persists_run_span_snapshot_and_artifact(db_session):
    set_trace_context(
        trace_id="trace-service-1",
        trace_type="retrieval",
        workspace_id=None,
        user_id=None,
        sampling_reason="unit-test",
    )
    service = TraceService(db_session)

    run = service.start_run(
        query_hash="hash-1",
        query_preview="what is openrag",
        search_config_snapshot={"top_k": 5},
    )
    assert run is not None
    assert run.trace_id == "trace-service-1"
    assert run.status == "running"

    span = service.start_span("retrieval.request", input_summary={"top_k": 5})
    assert span is not None
    assert get_trace_context()["span_id"] == span.span_id

    snapshot = service.record_snapshot(
        stage="retrieval.chunk_search",
        rank=1,
        chunk_id="chunk-1",
        score=0.91,
        score_parts={"vector": 0.91},
        metadata={"source": "milvus"},
    )
    assert snapshot is not None
    assert snapshot.trace_id == "trace-service-1"
    assert snapshot.span_id == span.span_id

    artifact = service.record_artifact_ref(
        artifact_type="debug_json",
        bucket="trace",
        object_key="trace-service-1/debug.json",
        content_type="application/json",
        size_bytes=128,
        metadata={"sampled": True},
    )
    assert artifact is not None

    finished_span = service.finish_span(
        output_summary={"result_count": 1},
        metrics={"latency_ms": 3},
        artifact_refs={"debug": artifact.artifact_id},
    )
    assert finished_span is not None
    assert finished_span.status == "success"
    assert get_trace_context()["span_id"] is None

    finished_run = service.finish_run()
    assert finished_run is not None
    assert finished_run.status == "success"
    assert finished_run.duration_ms is not None

    assert db_session.query(TraceRun).filter_by(trace_id="trace-service-1").one().status == "success"
    assert db_session.query(TraceSpan).filter_by(span_id=span.span_id).one().status == "success"
    assert db_session.query(TraceSnapshot).count() == 1
    assert db_session.query(TraceArtifact).count() == 1


def test_trace_service_marks_run_and_span_failed(db_session):
    set_trace_context(trace_id="trace-service-2", trace_type="document_processing")
    service = TraceService(db_session)

    service.start_run()
    span = service.start_span("parse.document")

    failed_span = service.fail_span(error_message="parser exploded")
    failed_run = service.fail_run(error_stage="parse.document", error_message="parser exploded")

    assert failed_span is not None
    assert failed_span.span_id == span.span_id
    assert failed_span.status == "failed"
    assert failed_span.error_message == "parser exploded"
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert failed_run.error_stage == "parse.document"
    assert failed_run.error_message == "parser exploded"


def test_trace_service_write_failures_are_warning_only(caplog):
    class FailingSession:
        def add(self, obj):
            raise RuntimeError("database down")

        def rollback(self):
            self.rolled_back = True

    set_trace_context(trace_id="trace-service-3", trace_type="retrieval")
    service = TraceService(FailingSession())

    with caplog.at_level(logging.WARNING):
        result = service.start_run()

    assert result is None
    assert "TraceService write failed" in caplog.text
