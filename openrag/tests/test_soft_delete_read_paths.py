"""Soft-deleted rows are invisible to list / detail / by-tag / by-path / tree."""

from datetime import datetime, timezone
from fastapi import status

from openrag.api.main import app
from openrag.api.deps import get_current_user
from openrag.models import File

from tests.test_files_api import (  # noqa: F401
    client, db, test_user, test_workspace,
    override_get_current_user_factory,
)


def _mk(db, ws, owner_id, uri, *, tag=None, deleted=False):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=owner_id, workspace_id=ws.id,
             is_directory=False, size=3, mime_type="text/plain", tag=tag,
             deleted_at=datetime.now(timezone.utc) if deleted else None)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_list_excludes_soft_deleted(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        _mk(db, test_workspace, test_user.id, "/live.txt")
        _mk(db, test_workspace, test_user.id, "/dead.txt", deleted=True)
        r = client.get("/files/", params={"workspace_id": test_workspace.id})
        names = [it["name"] for it in r.json()["items"]]
        assert "live.txt" in names and "dead.txt" not in names
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_detail_and_content_404_for_soft_deleted(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/dead.txt", deleted=True)
        assert client.get(f"/files/{f.id}").status_code == status.HTTP_404_NOT_FOUND
        assert client.get(f"/files/{f.id}/content").status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_reprocess_404_for_soft_deleted(client, db, test_user, test_workspace):
    """Codex #3: /files/{id}/reprocess must not act on a soft-deleted file."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/dead.txt", deleted=True)
        r = client.post(f"/files/{f.id}/reprocess", json={})
        assert r.status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_share_link_access_404_after_soft_delete(client, db, test_user, test_workspace):
    """Codex round-4 范围确认: the public, unauthenticated share endpoint must 404 once
    the file is soft-deleted (and must not crash on a now-missing file row).

    The public link is created via ShareLinkManager directly, so the critical leak-point
    assertion (access_share_link) does NOT depend on the create-link permission flow.
    ShareLink is registered in openrag.models.__init__, so the in-memory DB has the table.
    """
    from openrag.services.share_manager import ShareLinkManager
    from openrag.services.file_deletion import utcnow

    f = _mk(db, test_workspace, test_user.id, "/s.txt")  # active
    link = ShareLinkManager(db).create_share_link(file_id=f.id, created_by=test_user.id)
    assert client.get(f"/share/{link.token}").status_code == status.HTTP_200_OK  # visible while active

    f.deleted_at = utcnow(); f.tag = None  # soft-delete
    db.commit()

    # the previously-issued PUBLIC link now 404s instead of leaking metadata
    assert client.get(f"/share/{link.token}").status_code == status.HTTP_404_NOT_FOUND

    # creating a NEW share link for the soft-deleted file is also rejected. The share_api
    # 404 (missing file) is raised BEFORE the permission check, so this is 404 regardless
    # of the fixture's permission model.
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        assert client.post(
            "/share/links", json={"file_id": f.id}
        ).status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_share_manager_create_rejects_soft_deleted(db, test_user, test_workspace):
    """Codex LIKE-review follow-up: the active filter also lives in the manager layer, so a
    DIRECT ShareLinkManager.create_share_link (bypassing the API) cannot mint a public link
    for a soft-deleted file."""
    import pytest
    from openrag.services.share_manager import ShareLinkManager
    from openrag.services.file_deletion import utcnow

    f = _mk(db, test_workspace, test_user.id, "/m.txt")
    f.deleted_at = utcnow(); f.tag = None
    db.commit()
    with pytest.raises(ValueError):
        ShareLinkManager(db).create_share_link(file_id=f.id, created_by=test_user.id)
