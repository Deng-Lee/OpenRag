from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import openrag.models  # noqa: F401 - register all models
from openrag.api.deps import get_current_user, get_db
from openrag.api.main import app
from openrag.models import EvalResult, EvalRun
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


def test_eval_dataset_query_judgment_import_and_run_lifecycle(client, db_session, monkeypatch):
    def fake_default_search(self, *, query, user_id, workspace_id, **config):
        return {
            "results": [{"chunk_id": "chunk-a", "file_id": 11, "score": 0.95}],
            "stage_snapshots": {
                "vector": [{"chunk_id": "chunk-a", "file_id": 11, "score": 0.95}],
                "rerank": [{"chunk_id": "chunk-a", "file_id": 11, "score": 0.99}],
            },
        }

    monkeypatch.setattr(
        "openrag.services.eval_service.EvalService._default_search",
        fake_default_search,
    )

    created_dataset = client.post(
        "/eval/datasets",
        json={
            "name": "mini",
            "workspace_id": 10,
            "description": "mini eval",
            "metadata": {"suite": "api"},
        },
    )
    assert created_dataset.status_code == 201
    dataset_id = created_dataset.json()["id"]

    listed = client.get("/eval/datasets", params={"workspace_id": 10})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == dataset_id

    fetched = client.get(f"/eval/datasets/{dataset_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "mini"

    created_query = client.post(
        "/eval/queries",
        json={
            "dataset_id": dataset_id,
            "query_text": "alpha",
            "query_type": "policy",
            "expected_answer": "answer",
        },
    )
    assert created_query.status_code == 201
    query_id = created_query.json()["id"]

    created_judgment = client.post(
        "/eval/judgments",
        json={
            "dataset_id": dataset_id,
            "eval_query_id": query_id,
            "chunk_id": "chunk-a",
            "file_id": 11,
            "relevance_grade": 3,
        },
    )
    assert created_judgment.status_code == 201
    assert created_judgment.json()["weight"] == 1.0

    imported = client.post(
        "/eval/import",
        json={
            "dataset_id": dataset_id,
            "items": [
                {
                    "query_text": "beta",
                    "judgments": [
                        {
                            "chunk_id": "chunk-b",
                            "file_id": 12,
                            "relevance_grade": 2,
                            "source": "business_import",
                        }
                    ],
                }
            ],
        },
    )
    assert imported.status_code == 200
    assert imported.json() == {"query_count": 1, "judgment_count": 1}

    created_run = client.post(
        "/eval/runs",
        json={
            "dataset_id": dataset_id,
            "name": "run-a",
            "search_config_snapshot": {"top_k": 1},
            "execute": True,
        },
    )
    assert created_run.status_code == 201
    run_payload = created_run.json()
    assert run_payload["status"] == "success"
    run_id = run_payload["id"]

    runs = client.get("/eval/runs", params={"dataset_id": dataset_id})
    assert runs.status_code == 200
    assert runs.json()["items"][0]["id"] == run_id

    fetched_run = client.get(f"/eval/runs/{run_id}")
    assert fetched_run.status_code == 200
    assert fetched_run.json()["id"] == run_id

    results = client.get(f"/eval/runs/{run_id}/results")
    assert results.status_code == 200
    result_payload = results.json()
    assert result_payload["eval_run_id"] == run_id
    assert any(item["metric_scope"] == "query" for item in result_payload["items"])
    assert result_payload["summary"]["metrics"]["precision@1"] == pytest.approx(0.5)

    baseline = EvalRun(
        dataset_id=dataset_id,
        name="baseline",
        status="success",
        search_config_snapshot={"top_k": 1},
        started_at=datetime(2026, 5, 1, 10, 0, 0),
    )
    db_session.add(baseline)
    db_session.commit()
    db_session.refresh(baseline)
    db_session.add(
        EvalResult(
            eval_run_id=baseline.id,
            metric_scope="run_summary",
            source_scope="weighted_all",
            metrics={"precision@1": 0.25, "failed_count": 0},
        )
    )
    db_session.commit()

    compared = client.get(f"/eval/runs/{run_id}/compare", params={"baseline_id": baseline.id})
    assert compared.status_code == 200
    diff = compared.json()["diff"]
    assert diff["precision@1"]["current"] == pytest.approx(0.5)
    assert diff["precision@1"]["baseline"] == pytest.approx(0.25)
    assert diff["precision@1"]["delta"] == pytest.approx(0.25)


def test_openapi_contains_eval_routes(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in [
        "/eval/datasets",
        "/eval/datasets/{dataset_id}",
        "/eval/queries",
        "/eval/judgments",
        "/eval/import",
        "/eval/runs",
        "/eval/runs/{eval_run_id}",
        "/eval/runs/{eval_run_id}/results",
        "/eval/runs/{eval_run_id}/compare",
    ]:
        assert path in paths
