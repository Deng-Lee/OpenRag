"""Service-token tag tests: upload echo, duplicate-tag no-empty-dirs, GET by-tag.

Reuses the module-level fixtures from tests.test_service_api (shared in-memory
engine, app startup stub, service-token headers).
"""

from unittest.mock import patch

from sqlalchemy.orm import Session

from openrag.models import File as DbFile, ServiceToken, ServiceTokenWorkspace, Workspace
from openrag.storage.minio_storage import MinioStorage

from tests.test_service_api import (  # noqa: F401  (fixtures used by pytest)
    _root,
    _stub_app_startup,
    client,
    db,
    owner,
    workspace,
    service_token_headers,
    service_token_write_headers,
)


def _upload(client, ws_name, headers, *, filename="n.txt", tag=None, path="/", create_dirs=False):
    files = {"file": (filename, b"hello", "text/plain")}
    data = {"path": path}
    if tag is not None:
        data["tag"] = tag
    if create_dirs:
        data["create_dirs"] = "true"
    with patch.object(MinioStorage, "put_file", return_value=None):
        return client.post(
            f"/service/v1/workspaces/{ws_name}/documents",
            files=files, data=data, headers=headers,
        )


def _file_exists(db: Session, workspace_id: int, uri: str) -> bool:
    return db.query(DbFile).filter(DbFile.workspace_id == workspace_id, DbFile.uri == uri).first() is not None


def _insert_tagged_file(db: Session, ws: Workspace, owner, *, uri: str, tag: str) -> DbFile:
    f = DbFile(
        uri=uri,
        name=uri.rsplit("/", 1)[-1],
        owner_id=owner.id,
        workspace_id=ws.id,
        is_directory=False,
        size=3,
        mime_type="text/plain",
        tag=tag,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _read_headers_for_workspace(db: Session, owner, workspace: Workspace, secret: str) -> dict[str, str]:
    tok = ServiceToken(secret=secret, name=secret, created_by_user_id=owner.id)
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=workspace.id, permission="read"))
    db.commit()
    return {"X-OpenRag-Token": secret}


def test_service_upload_with_tag_echoes(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    r = _upload(client, workspace.name, service_token_write_headers, tag="svc-tag-1")
    assert r.status_code == 201
    assert r.json().get("tag") == "svc-tag-1"


def test_service_duplicate_tag_with_create_dirs_does_not_create_empty_dirs(
    client, db, workspace, owner, service_token_write_headers
):
    _root(db, workspace, owner)
    first = _upload(client, workspace.name, service_token_write_headers, filename="a.txt", tag="dup")
    assert first.status_code == 201

    second = _upload(
        client,
        workspace.name,
        service_token_write_headers,
        filename="b.txt",
        tag="dup",
        path="/new/deep",
        create_dirs=True,
    )
    assert second.status_code == 409
    db.expire_all()
    assert not _file_exists(db, workspace.id, "/new")
    assert not _file_exists(db, workspace.id, "/new/deep")


def test_get_by_tag_hit(client, db, workspace, owner, service_token_write_headers, service_token_headers):
    _root(db, workspace, owner)
    _upload(client, workspace.name, service_token_write_headers, filename="hit.txt", tag="findme")
    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "findme"}, headers=service_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tag"] == "findme"
    assert body["name"] == "hit.txt"


def test_get_by_tag_same_tag_is_workspace_scoped(client, db, workspace, owner, service_token_headers):
    other = Workspace(name="OtherWS", slug="other-ws", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    a = _insert_tagged_file(db, workspace, owner, uri="/a.txt", tag="shared")
    b = _insert_tagged_file(db, other, owner, uri="/b.txt", tag="shared")
    other_headers = _read_headers_for_workspace(db, owner, other, "sk-other-read")

    r1 = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "shared"}, headers=service_token_headers,
    )
    assert r1.status_code == 200
    assert r1.json()["id"] == a.id
    assert r1.json()["path"] == "/a.txt"

    r2 = client.get(
        f"/service/v1/workspaces/{other.name}/documents/by-tag",
        params={"tag": "shared"}, headers=other_headers,
    )
    assert r2.status_code == 200
    assert r2.json()["id"] == b.id
    assert r2.json()["path"] == "/b.txt"


def test_get_by_tag_unknown_404(client, db, workspace, service_token_headers):
    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "nope"}, headers=service_token_headers,
    )
    assert r.status_code == 404


def test_get_by_tag_cross_workspace_403(client, db, owner, workspace, service_token_headers):
    other = Workspace(name="OtherWS", slug="other-ws", owner_id=owner.id)
    db.add(other)
    db.commit()
    r = client.get(
        f"/service/v1/workspaces/{other.name}/documents/by-tag",
        params={"tag": "x"}, headers=service_token_headers,
    )
    assert r.status_code == 403
