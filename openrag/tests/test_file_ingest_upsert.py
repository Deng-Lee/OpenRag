"""upsert_file_by_tag: create / update-in-place / move+replace branches (service layer)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, Task, User, Workspace
from openrag.services import file_ingest
from openrag.services.file_ingest import upsert_file_by_tag


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def wsowner(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


@pytest.fixture()
def stub_minio(monkeypatch):
    """Stub MinIO so service-layer tests never touch object storage."""
    class _M:
        def put_file(self, *a, **k): return None
        def remove_file(self, *a, **k): return None
        def remove_document_hierarchy(self, *a, **k): return None
    # patch every module that constructs MinioStorage or deletes vectors on the upsert path:
    # - file_ingest.MinioStorage: move helper put/remove + create via ingest_new_file
    # - file_ingest.delete_milvus_vectors_for_file: move helper post-commit old-vector cleanup
    # - files_api.MinioStorage / files_api.delete_milvus_vectors_for_file: update branch goes
    #   through replace_file_content -> cleanup_file_processing_data (defined in files_api)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr("openrag.services.file_ingest.delete_milvus_vectors_for_file", lambda *a, **k: [])
    monkeypatch.setattr("openrag.api.files_api.MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr("openrag.api.files_api.delete_milvus_vectors_for_file", lambda *a, **k: [])
    return _M()


def test_upsert_creates_when_tag_absent(db, wsowner, stub_minio):
    w, u = wsowner
    f, task, action = upsert_file_by_tag(
        db, w, u.id,
        tag="report",
        target_path="/r.txt",
        file_content=b"hello",
        content_type="text/plain",
        parser_type="txt",
        create_dirs=True,
    )
    assert action == "created"
    assert f.tag == "report"
    assert f.uri == "/r.txt"
    assert f.deleted_at is None
    assert task is not None  # process_document enqueued
    assert db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).count() == 1


def test_upsert_updates_in_place_when_tag_and_path_match(db, wsowner, stub_minio):
    w, u = wsowner
    # seed an existing tagged file at /r.txt
    f0, _, a0 = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert a0 == "created"
    original_id = f0.id

    f1, task, action = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v2-longer", content_type="text/plain", parser_type="txt",
    )
    assert action == "updated"
    assert f1.id == original_id          # same row reused
    assert f1.uri == "/r.txt"
    assert f1.tag == "report"
    assert f1.size == len(b"v2-longer")  # content metadata refreshed
    assert task is not None
    # still exactly one active row with this tag
    assert db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).count() == 1


def test_upsert_update_ignores_multipart_filename(db, wsowner, stub_minio):
    """spec §4.8: same target_path is UPDATE even if the multipart filename differs;
    uri is driven by target_path, never by the uploaded filename."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # upsert_file_by_tag takes no filename param; target_path alone decides the uri.
    f1, _, action = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v2", content_type="text/plain", parser_type="txt",
    )
    assert action == "updated"
    assert f1.id == f0.id and f1.uri == "/r.txt"


def test_upsert_moves_and_replaces_when_path_differs(db, wsowner, stub_minio):
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    original_id = f0.id

    f1, task, action = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/archive/r2.txt",
        file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert action == "moved"
    assert f1.id == original_id          # SAME row relocated, not a new one (tag/id preserved)
    assert f1.uri == "/archive/r2.txt"
    assert f1.name == "r2.txt"
    assert f1.tag == "report"
    assert task is not None
    # exactly one active row with the tag; old uri no longer has an active row
    assert db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).count() == 1
    assert db.query(File).filter(File.uri == "/r.txt", File.deleted_at.is_(None)).count() == 0


def test_upsert_move_rejects_when_target_uri_occupied_by_other_file(db, wsowner, stub_minio):
    w, u = wsowner
    # tagged doc at /r.txt
    upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # an unrelated active file already sits at /archive/r2.txt (no tag).
    # Do NOT create the /archive directory row here: this regression proves the
    # conflict check happens before create_dirs/ensure_directory_path can commit
    # any side-effect directory.
    other = File(uri="/archive/r2.txt", name="r2.txt", owner_id=u.id, workspace_id=w.id,
                 is_directory=False, size=1)
    db.add(other); db.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 409
    # original tag/doc still reachable at its old uri
    assert db.query(File).filter(File.uri == "/r.txt", File.tag == "report",
                                 File.deleted_at.is_(None)).count() == 1
    # conflict path must not create /archive as a side effect
    assert db.query(File).filter(File.uri == "/archive", File.is_directory.is_(True),
                                 File.deleted_at.is_(None)).count() == 0


def test_upsert_move_rejects_pending_deleted_ancestor(db, wsowner, stub_minio):
    """Phase 2 write-guard: cannot relocate a tagged doc into a soft-deleted subtree."""
    w, u = wsowner
    upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # /archive directory exists but is soft-deleted (pending cleanup)
    from openrag.services.file_deletion import utcnow
    arch = File(uri="/archive", name="archive", owner_id=u.id, workspace_id=w.id,
                is_directory=True, size=0, deleted_at=utcnow())
    db.add(arch); db.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 409


def test_upsert_move_requires_existing_parent_without_create_dirs(db, wsowner, stub_minio):
    """P2-a / spec §4.9: relocating to a path whose parent dir is not an active directory
    must 400 when create_dirs is not set (mirrors service-upload strict parent semantics)."""
    w, u = wsowner
    upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/missing/b.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt",  # create_dirs defaults False
        )
    assert ei.value.status_code == 400
    # original remains reachable & unmoved
    assert db.query(File).filter(File.uri == "/r.txt", File.tag == "report",
                                 File.deleted_at.is_(None)).count() == 1


def test_upsert_move_minio_put_failure_keeps_old_reachable(db, wsowner, stub_minio, monkeypatch):
    """spec §4.8/§6: if writing the NEW object fails, the DB is untouched and the original
    tag still resolves to the original document at its original uri (no lost-doc window)."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    file_ingest.ensure_directory_path(db, w, "/archive")  # parent exists so we reach put_file

    class _PutBoom:
        def put_file(self, *a, **k): raise RuntimeError("minio down")
        def remove_file(self, *a, **k): return None
        def remove_document_hierarchy(self, *a, **k): return None
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", lambda *a, **k: _PutBoom())

    with pytest.raises(RuntimeError):
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt",
        )
    db.expire_all()
    row = db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).first()
    assert row is not None and row.id == f0.id and row.uri == "/r.txt"  # original intact


def test_upsert_move_db_failure_rolls_back_and_keeps_old_reachable(db, wsowner, stub_minio, monkeypatch):
    """spec §4.8/§6: if the move's single-commit transaction fails, the row rolls back to
    its original uri/tag (never deleted), so the tag still resolves to a reachable doc."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    file_ingest.ensure_directory_path(db, w, "/archive")

    def _boom(*a, **k):
        raise RuntimeError("db write failed")
    monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

    with pytest.raises(RuntimeError):
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt",
        )
    db.expire_all()
    row = db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).first()
    assert row is not None and row.id == f0.id and row.uri == "/r.txt"  # rolled back, reachable


def test_upsert_move_commit_failure_rolls_back_and_cleans_new_object(db, wsowner, monkeypatch):
    """spec §4.8/§6: a REAL db.commit() failure inside the move transaction must roll back
    (row keeps its old uri/tag, never deleted) AND best-effort remove the just-written
    target object. Uses a recording MinIO stub to assert the new object is cleaned up.

    Distinct from the add_task-failure case above: that fails BEFORE commit; this fails the
    commit itself, which is the path spec §4.8 step (4) calls out explicitly.
    """
    w, u = wsowner
    put_calls: list = []
    removed: list = []

    class _RecMinio:
        def put_file(self, bucket, key, *a, **k): put_calls.append(key)
        def remove_file(self, bucket, key, *a, **k): removed.append(key)
        def remove_document_hierarchy(self, *a, **k): return None

    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", lambda *a, **k: _RecMinio())
    monkeypatch.setattr("openrag.services.file_ingest.delete_milvus_vectors_for_file", lambda *a, **k: [])
    monkeypatch.setattr("openrag.api.files_api.MinioStorage", lambda *a, **k: _RecMinio())
    monkeypatch.setattr("openrag.api.files_api.delete_milvus_vectors_for_file", lambda *a, **k: [])

    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # pre-create the parent so the move uses create_dirs=False (no internal commit before
    # the helper's own commit), then make that commit fail.
    file_ingest.ensure_directory_path(db, w, "/archive")

    def _commit_boom():
        raise RuntimeError("commit failed")
    monkeypatch.setattr(db, "commit", _commit_boom)

    with pytest.raises(RuntimeError):
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt",  # create_dirs=False
        )
    db.expire_all()  # SELECT-only; no commit needed even with commit patched
    row = db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).first()
    assert row is not None and row.id == f0.id and row.uri == "/r.txt"  # rolled back, reachable
    assert "/archive/r2.txt" in put_calls   # new object was written first
    assert "/archive/r2.txt" in removed     # ...and best-effort removed after commit failure
