"""Pre-deployment acceptance checks for externally observable deletion semantics."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.deps import get_current_user, get_db
from openrag.api.main import app
from openrag.models import (
    Base,
    DocumentParseArtifact,
    File,
    User,
    Workspace,
    WorkspaceMember,
)
from openrag.services import file_deletion
from openrag.services.parse_artifact_service import ParseArtifactService


class InMemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_file(
        self,
        bucket_name: str,
        object_name: str,
        data: bytes,
        content_type: str | None = None,
    ) -> None:
        del content_type
        self.objects[(bucket_name, object_name.lstrip("/"))] = data

    def remove_file(self, bucket_name: str, object_name: str) -> None:
        self.objects.pop((bucket_name, object_name.lstrip("/")), None)

    def remove_document_hierarchy(self, bucket_name: str, file_uri: str) -> None:
        del bucket_name, file_uri

    def remove_directory(self, bucket_name: str, prefix: str) -> None:
        normalized = prefix.lstrip("/").rstrip("/") + "/"
        for key in list(self.objects):
            if key[0] == bucket_name and key[1].startswith(normalized):
                self.objects.pop(key)


def _stub_successful_vector_delete(monkeypatch) -> None:
    if hasattr(file_deletion, "delete_vectors_for_file_across_generations"):
        monkeypatch.setattr(
            file_deletion,
            "delete_vectors_for_file_across_generations",
            lambda _db, file_id: {
                "file_id": file_id,
                "target_generation_ids": ["acceptance-generation"],
                "succeeded_generation_ids": ["acceptance-generation"],
                "failed_generation_ids": [],
                "failed_subsystems": [],
            },
        )
        return
    monkeypatch.setattr(
        file_deletion,
        "delete_milvus_vectors_for_file",
        lambda _file_id: [],
    )


def _stub_failed_vector_delete(monkeypatch) -> None:
    if hasattr(file_deletion, "delete_vectors_for_file_across_generations"):
        error_type = file_deletion.GenerationDeletePropagationError
        result = {
            "file_id": 0,
            "target_generation_ids": ["acceptance-generation"],
            "succeeded_generation_ids": [],
            "failed_generation_ids": ["acceptance-generation"],
            "failed_subsystems": [],
        }

        def fail(_db, file_id):
            result["file_id"] = file_id
            raise error_type(result)

        monkeypatch.setattr(
            file_deletion,
            "delete_vectors_for_file_across_generations",
            fail,
        )
        return
    monkeypatch.setattr(
        file_deletion,
        "delete_milvus_vectors_for_file",
        lambda _file_id: ["milvus_chunk"],
    )


def test_sync_file_delete_removes_canonical_parse_artifacts(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(
        username="deploy-delete-user",
        email="deploy-delete@example.com",
        password_hash="hash",
        full_name="Deploy Delete",
        is_active=True,
    )
    db.add(user)
    db.commit()
    workspace = Workspace(
        name="Deploy Delete",
        slug="deploy-delete",
        owner_id=user.id,
    )
    db.add(workspace)
    db.commit()
    db.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role="write",
        )
    )
    file = File(
        uri="/Doc1.pdf",
        name="Doc1.pdf",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=4,
        mime_type="application/pdf",
    )
    db.add(file)
    db.commit()
    db.refresh(file)

    storage = InMemoryObjectStorage()
    storage.put_file(workspace.slug, file.uri, b"%PDF")
    source_key = (workspace.slug, file.uri.lstrip("/"))
    artifact = ParseArtifactService(db, storage).persist_parse_artifacts(
        workspace_id=workspace.id,
        file_id=file.id,
        bucket_name=workspace.slug,
        file_uri=file.uri,
        source_doc_bytes=b"%PDF",
        blocks=[{"text": "canonical content", "page": 1}],
        parser_name="PDFParser",
        parser_version="1",
    )
    canonical_keys = {
        (artifact.canonical_json_bucket, artifact.canonical_json_object_key),
        (artifact.canonical_md_bucket, artifact.canonical_md_object_key),
    }
    orphan_key = (
        workspace.slug,
        f"parse_artifacts/{file.id}/orphan/parser/v2/canonical.json",
    )
    neighbor_key = (
        workspace.slug,
        f"parse_artifacts/{file.id}0/keep/canonical.json",
    )
    storage.put_file(*orphan_key, b"orphan")
    storage.put_file(*neighbor_key, b"neighbor")
    assert canonical_keys <= set(storage.objects)

    monkeypatch.setattr(file_deletion, "MinioStorage", lambda: storage)
    monkeypatch.setattr(
        file_deletion,
        "HierarchyStorage",
        lambda: type(
            "HierarchyStub",
            (),
            {"delete_document_hierarchy": lambda *_args, **_kwargs: None},
        )(),
    )
    _stub_successful_vector_delete(monkeypatch)

    def override_db():
        yield db

    missing = object()
    previous_db_override = app.dependency_overrides.get(get_db, missing)
    previous_user_override = app.dependency_overrides.get(
        get_current_user, missing
    )
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).delete(
            f"/files/{file.id}", params={"background": "false"}
        )
        db.expire_all()
        file_exists_after_delete = db.get(File, file.id) is not None
        artifact_rows_after_delete = db.query(DocumentParseArtifact).count()
    finally:
        if previous_db_override is missing:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_db_override
        if previous_user_override is missing:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user_override
        db.close()
        Base.metadata.drop_all(engine)

    assert response.status_code == 200, response.text
    assert file_exists_after_delete is False
    assert artifact_rows_after_delete == 0
    assert source_key not in storage.objects
    assert canonical_keys.isdisjoint(storage.objects)
    assert orphan_key not in storage.objects
    assert neighbor_key in storage.objects


def test_sync_file_delete_fails_closed_when_vector_cleanup_fails(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(
        username="deploy-fail-closed-user",
        email="deploy-fail-closed@example.com",
        password_hash="hash",
        full_name="Deploy Fail Closed",
        is_active=True,
    )
    db.add(user)
    db.commit()
    workspace = Workspace(
        name="Deploy Fail Closed",
        slug="deploy-fail-closed",
        owner_id=user.id,
    )
    db.add(workspace)
    db.commit()
    file = File(
        uri="/Doc1.pdf",
        name="Doc1.pdf",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=4,
        mime_type="application/pdf",
    )
    db.add(file)
    db.commit()
    file_id = file.id

    storage = InMemoryObjectStorage()
    storage.put_file(workspace.slug, file.uri, b"%PDF")
    monkeypatch.setattr(file_deletion, "MinioStorage", lambda: storage)
    monkeypatch.setattr(
        file_deletion,
        "HierarchyStorage",
        lambda: type(
            "HierarchyStub",
            (),
            {"delete_document_hierarchy": lambda *_args, **_kwargs: None},
        )(),
    )
    _stub_failed_vector_delete(monkeypatch)

    try:
        with pytest.raises(Exception):
            file_deletion.delete_file_with_storage(db, file, workspace)
        db.expire_all()
        assert db.get(File, file_id) is not None
        assert (workspace.slug, file.uri.lstrip("/")) in storage.objects
    finally:
        db.close()
        Base.metadata.drop_all(engine)
