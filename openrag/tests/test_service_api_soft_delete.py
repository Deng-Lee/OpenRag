"""External DELETE by-path: async soft-delete (202) frees tag + hides doc."""

import pytest

from openrag.models import File, Task, Workspace

from tests.test_service_api import (  # noqa: F401
    _root, _stub_app_startup, client, db, owner, workspace,
    service_token_headers, service_token_write_headers,
)


def _mk(db, ws, owner, uri, *, tag=None):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=owner.id,
             workspace_id=ws.id, is_directory=False, size=3, mime_type="text/plain", tag=tag)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_delete_by_path_async_soft_deletes(client, db, workspace, owner, service_token_write_headers):
    f = _mk(db, workspace, owner, "/d.txt", tag="dt")
    r = client.request(
        "DELETE",
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/d.txt"},
        headers=service_token_write_headers,
    )
    assert r.status_code == 202
    db.expire_all()
    row = db.query(File).filter(File.id == f.id).first()
    assert row.deleted_at is not None and row.tag is None
    assert db.query(Task).filter(Task.file_id == f.id, Task.task_type == "delete_file").count() == 1


def test_delete_by_path_unknown_404(client, db, workspace, service_token_write_headers):
    r = client.request(
        "DELETE",
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/nope.txt"},
        headers=service_token_write_headers,
    )
    assert r.status_code == 404


def test_delete_by_path_requires_write_403(client, db, workspace, owner, service_token_headers):
    _mk(db, workspace, owner, "/d.txt", tag="dt")
    r = client.request(
        "DELETE",
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/d.txt"},
        headers=service_token_headers,  # read-only token
    )
    assert r.status_code == 403


def test_delete_by_path_rollback_when_add_task_fails(client, db, workspace, owner, service_token_write_headers, monkeypatch):
    """Codex #4: external by-path soft-delete rolls back fully if enqueue fails."""
    f = _mk(db, workspace, owner, "/d.txt", tag="dt")

    def _boom(*a, **k):
        raise RuntimeError("enqueue failed")
    monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

    with pytest.raises(RuntimeError):
        client.request(
            "DELETE",
            f"/service/v1/workspaces/{workspace.name}/documents/by-path",
            params={"path": "/d.txt"},
            headers=service_token_write_headers,
        )
    db.expire_all()
    row = db.query(File).filter(File.id == f.id).first()
    assert row.deleted_at is None and row.tag == "dt"  # rolled back
    assert db.query(Task).filter(Task.file_id == f.id).count() == 0


def test_get_by_tag_and_by_path_hide_soft_deleted(client, db, workspace, owner, service_token_headers):
    f = _mk(db, workspace, owner, "/dead.txt", tag="dt")
    # soft-delete directly
    from openrag.services.file_deletion import utcnow
    f.deleted_at = utcnow(); f.tag = None
    db.commit()
    assert client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "dt"}, headers=service_token_headers,
    ).status_code == 404
    assert client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/dead.txt"}, headers=service_token_headers,
    ).status_code == 404
