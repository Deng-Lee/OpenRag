from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import openrag.models  # noqa: F401 - register all models
from openrag.api.deps import get_current_user, get_db
from openrag.api.main import app
from openrag.models import TraceRun, TraceSnapshot, TraceSpan
from openrag.models.base import Base
from openrag.models.user import User


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session

    def override_current_user():
        return User(
            id=1,
            username="admin",
            email="admin@example.com",
            password_hash="hash",
            full_name="Admin",
            is_active=True,
            is_admin=True,
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_list_traces_filters_by_trace_metadata(client, db_session):
    matching = TraceRun(
        trace_id="trace-api-1",
        trace_type="retrieval",
        workspace_id=10,
        file_id=101,
        task_id="task-1",
        eval_run_id=7,
        eval_query_id=8,
        query_hash="query-hash-1",
        query_preview="外汇政策",
        status="failed",
        started_at=datetime(2026, 5, 1, 10, 0, 0),
        ended_at=datetime(2026, 5, 1, 10, 0, 1),
        duration_ms=1000,
        error_stage="retrieval.rerank",
        error_message="rerank failed",
    )
    other = TraceRun(
        trace_id="trace-api-2",
        trace_type="upload",
        workspace_id=99,
        status="success",
        started_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([matching, other])
    db_session.commit()

    response = client.get(
        "/traces",
        params={
            "trace_type": "retrieval",
            "workspace_id": 10,
            "file_id": 101,
            "task_id": "task-1",
            "eval_run_id": 7,
            "eval_query_id": 8,
            "query_hash": "query-hash-1",
            "started_at_from": "2026-05-01T00:00:00",
            "started_at_to": "2026-05-02T00:00:00",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["trace_id"] == "trace-api-1"
    assert payload["items"][0]["error"]["stage"] == "retrieval.rerank"
    assert payload["items"][0]["error"]["message"] == "rerank failed"


def test_get_trace_detail_and_top50_stage_snapshots(client, db_session):
    run = TraceRun(
        trace_id="trace-api-detail",
        trace_type="eval_retrieval",
        workspace_id=10,
        eval_run_id=5,
        eval_query_id=6,
        status="failed",
        started_at=datetime(2026, 5, 1, 10, 0, 0),
        error_stage="eval.search",
        error_message="search unavailable",
    )
    span = TraceSpan(
        span_id="span-api-1",
        trace_id="trace-api-detail",
        stage="eval.search",
        status="failed",
        started_at=datetime(2026, 5, 1, 10, 0, 0),
        error_message="search unavailable",
    )
    db_session.add_all([run, span])
    for rank in range(1, 56):
        db_session.add(
            TraceSnapshot(
                trace_id="trace-api-detail",
                span_id="span-api-1",
                stage="rerank",
                rank=rank,
                chunk_id=f"chunk-{rank}",
                file_id=rank,
                score=1.0 / rank,
                metadata_={"source": "unit"},
            )
        )
    db_session.add(
        TraceSnapshot(
            trace_id="trace-api-detail",
            span_id="span-api-1",
            stage="vector",
            rank=1,
            chunk_id="vector-only",
        )
    )
    db_session.commit()

    detail = client.get("/traces/trace-api-detail")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["run"]["trace_id"] == "trace-api-detail"
    assert detail_payload["error"]["stage"] == "eval.search"
    assert detail_payload["spans"][0]["stage"] == "eval.search"

    snapshots = client.get("/traces/trace-api-detail/snapshots", params={"stage": "rerank"})
    assert snapshots.status_code == 200
    snapshot_payload = snapshots.json()
    assert snapshot_payload["trace_id"] == "trace-api-detail"
    assert snapshot_payload["stage"] == "rerank"
    assert len(snapshot_payload["items"]) == 50
    assert snapshot_payload["items"][0]["rank"] == 1
    assert snapshot_payload["items"][-1]["rank"] == 50


def test_openapi_contains_trace_routes(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/traces" in paths
    assert "/traces/{trace_id}" in paths
    assert "/traces/{trace_id}/snapshots" in paths
