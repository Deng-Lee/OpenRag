"""Internal DELETE /files/{id}: async soft-delete (file row / directory subtree)."""

import pytest
from fastapi import status

from openrag.api.main import app
from openrag.api.deps import get_current_user
from openrag.models import File, Task

from tests.test_files_api import (  # noqa: F401
    client, db, test_user, test_workspace, FakeMinioStorage,
    override_get_current_user_factory,
)


def _mk(db, ws, owner_id, uri, *, is_dir=False, tag=None):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1] or "root", owner_id=owner_id,
             workspace_id=ws.id, is_directory=is_dir, size=0, tag=tag)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_delete_file_async_soft_deletes_and_frees_tag(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/a.txt", tag="t1")
        r = client.delete(f"/files/{f.id}")  # background=True default
        assert r.status_code == status.HTTP_202_ACCEPTED
        db.expire_all()
        row = db.query(File).filter(File.id == f.id).first()
        assert row.deleted_at is not None and row.tag is None
        assert db.query(Task).filter(Task.file_id == f.id, Task.task_type == "delete_file").count() == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_directory_async_soft_deletes_subtree_and_enqueues_prefix(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        d = _mk(db, test_workspace, test_user.id, "/dir", is_dir=True)
        a = _mk(db, test_workspace, test_user.id, "/dir/a.txt", tag="x")
        r = client.delete(f"/files/{d.id}")
        assert r.status_code == status.HTTP_202_ACCEPTED
        db.expire_all()
        for fid in (d.id, a.id):
            assert db.query(File).filter(File.id == fid).first().deleted_at is not None
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_file_rollback_when_add_task_fails(client, db, test_user, test_workspace, monkeypatch):
    """Codex #4: if add_task raises, deleted_at/tag/task all roll back (file stays active)."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/a.txt", tag="t1")

        def _boom(*a, **k):
            raise RuntimeError("enqueue failed")
        monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

        # TestClient(raise_server_exceptions=True default) re-raises the unhandled error;
        # the helper has already rolled back before propagating.
        with pytest.raises(RuntimeError):
            client.delete(f"/files/{f.id}")
        db.expire_all()
        row = db.query(File).filter(File.id == f.id).first()
        assert row.deleted_at is None and row.tag == "t1"  # unchanged
        assert db.query(Task).filter(Task.file_id == f.id).count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_directory_rollback_when_add_task_fails(client, db, test_user, test_workspace, monkeypatch):
    """Codex #4: directory subtree soft-delete rolls back fully if enqueue fails."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        d = _mk(db, test_workspace, test_user.id, "/dir", is_dir=True)
        a = _mk(db, test_workspace, test_user.id, "/dir/a.txt", tag="x")

        def _boom(*a, **k):
            raise RuntimeError("enqueue failed")
        monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

        with pytest.raises(RuntimeError):
            client.delete(f"/files/{d.id}")
        db.expire_all()
        for fid in (d.id, a.id):
            row = db.query(File).filter(File.id == fid).first()
            assert row.deleted_at is None  # rolled back
        assert db.query(File).filter(File.id == a.id).first().tag == "x"
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_root_directory_returns_400(client, db, test_user, test_workspace):
    """Codex round-3 #2: deleting the workspace root directory must be rejected."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        root = _mk(db, test_workspace, test_user.id, "/", is_dir=True)
        child = _mk(db, test_workspace, test_user.id, "/keep.txt", tag="k")
        r = client.delete(f"/files/{root.id}")
        assert r.status_code == status.HTTP_400_BAD_REQUEST
        db.expire_all()
        kept = db.query(File).filter(File.id == child.id).first()
        assert kept.deleted_at is None and kept.tag == "k"  # nothing soft-deleted
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_path_prefix_async_soft_deletes_and_writes_watermark(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        a = _mk(db, test_workspace, test_user.id, "/docs/a.txt", tag="p1")
        b = _mk(db, test_workspace, test_user.id, "/docs/sub/b.txt", tag="p2")
        r = client.post(
            "/files/delete-path-prefix",
            params={"background": "true"},
            json={"workspace_id": test_workspace.id, "path": "/docs"},
        )
        assert r.status_code == status.HTTP_202_ACCEPTED
        db.expire_all()
        for fid in (a.id, b.id):
            row = db.query(File).filter(File.id == fid).first()
            assert row.deleted_at is not None and row.tag is None
        task = db.query(Task).filter(Task.task_type == "delete_path_prefix").first()
        assert task is not None
        assert "deleted_before" in (task.payload or {})
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_path_prefix_rollback_when_add_task_fails(client, db, test_user, test_workspace, monkeypatch):
    """Codex #4: prefix soft-delete rolls back fully if enqueue fails (no rows marked)."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        a = _mk(db, test_workspace, test_user.id, "/docs/a.txt", tag="p1")

        def _boom(*a, **k):
            raise RuntimeError("enqueue failed")
        monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

        with pytest.raises(RuntimeError):
            client.post(
                "/files/delete-path-prefix",
                params={"background": "true"},
                json={"workspace_id": test_workspace.id, "path": "/docs"},
            )
        db.expire_all()
        row = db.query(File).filter(File.id == a.id).first()
        assert row.deleted_at is None and row.tag == "p1"  # rolled back
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_path_prefix_count_escapes_like_wildcards(client, db, test_user, test_workspace):
    """Codex LIKE review: count query must not treat '_' as wildcard and enqueue falsely."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        neighbor = _mk(db, test_workspace, test_user.id, "/axb/two.txt", tag="neighbor")
        r = client.post(
            "/files/delete-path-prefix",
            params={"background": "true"},
            json={"workspace_id": test_workspace.id, "path": "/a_b"},
        )
        assert r.status_code == status.HTTP_200_OK
        assert "No files" in r.json()["message"]
        db.expire_all()
        row = db.query(File).filter(File.id == neighbor.id).first()
        assert row.deleted_at is None and row.tag == "neighbor"
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)
