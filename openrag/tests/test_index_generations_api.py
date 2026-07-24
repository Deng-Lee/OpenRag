from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.deps import get_current_user, get_db
from openrag.api.index_generations_api import get_provision_service, router
from openrag.models import AuditLog, Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute
from openrag.models.user import User
from openrag.services.index_generation_service import IndexGenerationService


def _active_values():
    return {
        "scope": "global",
        "embedding_provider": "provider",
        "embedding_model": "active-model",
        "embedding_revision": "active-revision",
        "embedding_dimension": 3,
        "embedding_fingerprint": "a" * 64,
        "embedding_config_ref": "embedding/active",
        "vector_normalization": "none",
        "distance_metric": "COSINE",
        "schema_version": 1,
        "chunk_policy_revision": "chunk-v1",
        "hierarchy_policy_revision": "hierarchy-v1",
        "chunk_collection_name": "chunks_active",
        "layer_collection_name": "layers_active",
        "manifest": {"legacy": True},
    }


def request_body(**updates):
    values = {
        "client_request_id": "candidate-request-0001",
        "embedding_config_ref": "embedding/candidate",
        "embedding_model": "candidate-model",
        "embedding_revision": "candidate-revision",
        "embedding_dimension": 3,
        "schema_version": 2,
        "chunk_policy_revision": "chunk-v2",
        "hierarchy_policy_revision": "hierarchy-v2",
        "rollback_window_seconds": 3600,
        "force_rebuild": False,
    }
    values.update(updates)
    return values


def setup_app():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admin = User(
        username="index-admin",
        email="index-admin@example.com",
        password_hash="hash",
        full_name="Index Admin",
        is_active=True,
        is_admin=True,
    )
    member = User(
        username="member",
        email="member@example.com",
        password_hash="hash",
        full_name="Member",
        is_active=True,
        is_admin=False,
    )
    session.add_all([admin, member])
    session.flush()
    active = IndexGenerationService(session).create_generation(**_active_values())
    active.state = "active"
    session.add(
        IndexGenerationRoute(
            scope="global", active_generation_id=active.id, route_version=1
        )
    )
    session.commit()
    app = FastAPI()
    app.include_router(router)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    return app, session, admin, member


def test_non_admin_write_is_forbidden():
    app, session, _admin, member = setup_app()
    app.dependency_overrides[get_current_user] = lambda: member

    response = TestClient(app).post("/index-generations", json=request_body())

    assert response.status_code == 403
    assert session.query(IndexGeneration).count() == 1


def test_preview_is_read_only_and_create_is_idempotent():
    app, session, _admin, _member = setup_app()
    client = TestClient(app)

    preview = client.post("/index-generations/preview", json=request_body())
    assert preview.status_code == 200
    assert session.query(IndexGeneration).count() == 1
    assert session.query(AuditLog).count() == 0

    first = client.post("/index-generations", json=request_body())
    second = client.post("/index-generations", json=request_body())

    assert first.status_code == second.status_code == 200
    assert first.json()["generation_id"] == preview.json()["generation_id"]
    assert second.json()["generation_id"] == first.json()["generation_id"]
    assert first.json()["state"] == "draft"
    assert session.query(IndexGeneration).count() == 2
    assert session.query(AuditLog).count() == 1


def test_reused_request_id_with_different_manifest_is_rejected():
    app, session, _admin, _member = setup_app()
    client = TestClient(app)
    assert client.post("/index-generations", json=request_body()).status_code == 200

    response = client.post(
        "/index-generations",
        json=request_body(chunk_policy_revision="different-policy"),
    )

    assert response.status_code == 409
    assert session.query(IndexGeneration).count() == 2


def test_provision_error_does_not_expose_backend_details():
    app, _session, _admin, _member = setup_app()
    client = TestClient(app)
    created = client.post("/index-generations", json=request_body()).json()

    class FailingProvisionService:
        def provision(self, _generation_id):
            raise RuntimeError("MILVUS_PASSWORD=do-not-leak")

    app.dependency_overrides[get_provision_service] = lambda: FailingProvisionService()
    response = client.post(
        f"/index-generations/{created['generation_id']}/provision",
        json={"client_request_id": "provision-request-0001"},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Candidate collection provisioning failed"}
    assert "do-not-leak" not in response.text
