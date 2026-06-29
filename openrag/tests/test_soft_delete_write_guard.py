"""Write guard: cannot write/move active rows into a soft-deleted (pending) subtree."""

import pytest
from fastapi import HTTPException, status

from openrag.api.main import app
from openrag.api.deps import get_current_user
from openrag.models import File

from tests.test_files_api import (  # noqa: F401
    client, db, test_user, test_workspace, FakeMinioStorage,
    override_get_current_user_factory,
)


def _soft_deleted_dir(db, ws, owner_id, uri):
    from openrag.services.file_deletion import utcnow
    d = File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=owner_id, workspace_id=ws.id,
             is_directory=True, size=0, deleted_at=utcnow())
    db.add(d); db.commit(); db.refresh(d)
    return d


def test_helper_flags_pending_ancestor(db, test_user, test_workspace):
    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
    with pytest.raises(HTTPException) as ei:
        assert_no_pending_deleted_ancestor(db, test_workspace.id, "/dir/sub")
    assert ei.value.status_code == status.HTTP_409_CONFLICT


def test_helper_allows_clean_path(db, test_user, test_workspace):
    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    # no soft-deleted ancestor -> no raise
    assert_no_pending_deleted_ancestor(db, test_workspace.id, "/clean/sub")


def test_upload_into_pending_dir_409(client, db, test_user, test_workspace, monkeypatch):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)
    try:
        _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
        r = client.post(
            "/files/upload",
            files={"file": ("n.txt", b"x", "text/plain")},
            data={"path": "/dir", "workspace_id": str(test_workspace.id)},
        )
        assert r.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_create_directory_under_pending_dir_409(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
        r = client.post(
            "/files/directories",
            data={"path": "/dir/new", "workspace_id": str(test_workspace.id)},
        )
        assert r.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_move_into_pending_dir_409(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
        src = File(uri="/src.txt", name="src.txt", owner_id=test_user.id,
                   workspace_id=test_workspace.id, is_directory=False, size=0)
        db.add(src); db.commit(); db.refresh(src)
        r = client.put(f"/files/{src.id}/move", json={"new_path": "/dir/src.txt"})
        assert r.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)
