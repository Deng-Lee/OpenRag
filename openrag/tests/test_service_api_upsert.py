"""External PUT upsert-by-tag: 201 create / 200 update / 200 move+replace, by tag."""

import io
import pytest

from openrag.models import File, Task
from openrag.storage.minio_storage import MinioStorage

from tests.test_service_api import (  # noqa: F401
    _assert_initial_task_quota, _root, _stub_app_startup, client, db, owner, workspace,
    service_token_headers, service_token_write_headers,
)


@pytest.fixture(autouse=True)
def _stub_storage(monkeypatch):
    """Service E2E must never touch real MinIO/Milvus. Stub every object-storage / vector
    call the upsert path can reach: create -> put_file; update -> replace_file_content ->
    cleanup_file_processing_data; move -> put_file + best-effort old object/hierarchy/vector
    cleanup. Mirrors the patch.object(MinioStorage, "put_file") convention the other service
    E2E tests use (the plan's test file omitted it, so real connections hung ~2min on retry)."""
    monkeypatch.setattr(MinioStorage, "put_file", lambda *a, **k: None)
    monkeypatch.setattr(MinioStorage, "remove_file", lambda *a, **k: None)
    monkeypatch.setattr(MinioStorage, "remove_document_hierarchy", lambda *a, **k: None)
    monkeypatch.setattr("openrag.api.files_api.cleanup_file_processing_data", lambda *a, **k: None)
    monkeypatch.setattr("openrag.services.file_ingest.delete_milvus_vectors_for_file", lambda *a, **k: [])


def _put_upsert(client, workspace, headers, *, tag, target_path, content, filename="upload.bin", parser="txt"):
    # tag + target_path are Form fields (spec §4.8); the multipart filename is decorative.
    return client.put(
        f"/service/v1/workspaces/{workspace.name}/documents/upsert-by-tag",
        data={"tag": tag, "target_path": target_path, "parser_type": parser, "create_dirs": "true"},
        files={"file": (filename, io.BytesIO(content), "text/plain")},
        headers=headers,
    )


def test_upsert_creates_returns_201(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/a.txt", content=b"hello")
    assert r.status_code == 201
    body = r.json()
    assert body["tag"] == "t1" and body["path"] == "/a.txt"
    assert body["action"] == "created"
    _assert_initial_task_quota(body)
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1


def test_upsert_update_in_place_returns_200(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    _put_upsert(client, workspace, service_token_write_headers,
                tag="t1", target_path="/a.txt", content=b"v1")
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/a.txt", content=b"v2-longer")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/a.txt" and body["action"] == "updated"
    _assert_initial_task_quota(body)
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1


def test_upsert_move_and_replace_returns_200(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    _put_upsert(client, workspace, service_token_write_headers,
                tag="t1", target_path="/a.txt", content=b"v1")
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/archive/b.txt", content=b"v2")
    assert r.status_code == 200
    assert r.json()["path"] == "/archive/b.txt" and r.json()["action"] == "moved"
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1
    assert db.query(File).filter(File.uri == "/a.txt", File.deleted_at.is_(None)).count() == 0


def test_upsert_target_path_drives_uri_not_multipart_filename(client, db, workspace, owner, service_token_write_headers):
    """spec §4.8/§8.5#10: the uri comes from Form target_path, NOT the multipart filename."""
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/archive/real.txt", content=b"v1", filename="ignored.txt")
    assert r.status_code == 201
    assert r.json()["path"] == "/archive/real.txt"
    assert db.query(File).filter(File.uri == "/archive/real.txt", File.deleted_at.is_(None)).count() == 1
    assert db.query(File).filter(File.name == "ignored.txt").count() == 0


def test_upsert_missing_target_path_returns_422(client, db, workspace, owner, service_token_write_headers):
    """target_path is a required Form field; omitting it is a 422 (not a silent default)."""
    _root(db, workspace, owner)
    r = client.put(
        f"/service/v1/workspaces/{workspace.name}/documents/upsert-by-tag",
        data={"tag": "t1", "parser_type": "txt", "create_dirs": "true"},  # no target_path
        files={"file": ("a.txt", io.BytesIO(b"v1"), "text/plain")},
        headers=service_token_write_headers,
    )
    assert r.status_code == 422


def test_upsert_requires_write_token(client, db, workspace, owner, service_token_headers):
    """read-only token cannot upsert."""
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_headers,
                    tag="t1", target_path="/a.txt", content=b"v1")
    assert r.status_code == 403
