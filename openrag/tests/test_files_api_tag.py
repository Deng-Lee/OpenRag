"""Internal JWT /files/upload: tag echo + API-layer duplicate-tag 409.

Reuses the module-level fixtures + helpers from tests.test_files_api (shared
in-memory engine, app dependency overrides, FakeMinioStorage).
"""

from fastapi import status

from openrag.api.main import app
from openrag.api.deps import get_current_user

from tests.test_files_api import (  # noqa: F401  (fixtures used by pytest)
    client,
    db,
    test_user,
    test_workspace,
    FakeMinioStorage,
    override_get_current_user_factory,
)


def test_upload_with_tag_persists_and_echoes(client, db, test_user, test_workspace, monkeypatch):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)

    try:
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/", "workspace_id": str(test_workspace.id), "tag": "doc-1"}
        r = client.post("/files/upload", files=files, data=data)

        assert r.status_code == status.HTTP_201_CREATED
        assert r.json().get("tag") == "doc-1"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_upload_duplicate_tag_returns_409(client, db, test_user, test_workspace, monkeypatch):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)

    try:
        first = client.post(
            "/files/upload",
            files={"file": ("a.txt", b"one", "text/plain")},
            data={"path": "/", "workspace_id": str(test_workspace.id), "tag": "dup"},
        )
        assert first.status_code == status.HTTP_201_CREATED

        second = client.post(
            "/files/upload",
            files={"file": ("b.txt", b"two", "text/plain")},
            data={"path": "/", "workspace_id": str(test_workspace.id), "tag": "dup"},
        )
        assert second.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)
