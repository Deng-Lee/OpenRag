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
