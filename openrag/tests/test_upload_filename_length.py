"""``ingest_new_file`` must reject over-long file names with a clean 400.

The worker downloads each uploaded file to a local temp path named
``doc_{file_id}_{basename}``; the OS caps a single path component at 255 bytes
(ENAMETOOLONG). Without an up-front check, an over-long name passes MinIO/DB
(MinIO keys allow ~1024 bytes; the ``name`` column is 255 *chars*) and then the
worker fails silently. The shared chokepoint must reject it before any write so
both the web ``/files/upload`` and the service-token upload return a clear 400.
"""

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace
from openrag.services import file_ingest
from openrag.services.file_ingest import MAX_FILENAME_BYTES, ingest_new_file


class _FakeMinio:
    def __init__(self):
        type(self).calls += 1

    def put_file(self, *args, **kwargs):
        return None


_FakeMinio.calls = 0


@pytest.fixture(scope="function")
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _patch_minio(monkeypatch):
    _FakeMinio.calls = 0
    monkeypatch.setattr(file_ingest, "MinioStorage", _FakeMinio)


@pytest.fixture
def owner(db_session: Session) -> User:
    u = User(
        username="len_owner",
        email="len@example.com",
        password_hash="h",
        full_name="L",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def workspace(db_session: Session, owner: User) -> Workspace:
    ws = Workspace(name="Len WS", slug="len-ws", owner_id=owner.id)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _ingest(db, ws, owner, *, filename, path="/"):
    return ingest_new_file(
        db,
        ws,
        owner.id,
        parent_logical_path=path,
        upload_filename=filename,
        file_content=b"hello",
        content_type="text/plain",
        require_parent_dir=False,
    )


def _file_count(db: Session, ws: Workspace) -> int:
    return (
        db.query(File)
        .filter(File.workspace_id == ws.id, File.is_directory.is_(False))
        .count()
    )


def test_rejects_overlong_ascii_name(db_session, workspace, owner):
    filename = "a" * (MAX_FILENAME_BYTES - 3) + ".txt"  # 201 bytes
    with pytest.raises(HTTPException) as ei:
        _ingest(db_session, workspace, owner, filename=filename)

    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST
    assert "too long" in ei.value.detail.lower()
    # Fast-fail before any storage/DB write: no MinIO put, no file row.
    assert _FakeMinio.calls == 0
    assert _file_count(db_session, workspace) == 0


def test_rejects_overlong_unicode_name_by_bytes(db_session, workspace, owner):
    # 67 Chinese chars = 201 UTF-8 bytes, but only 67 characters (< 255-char column).
    filename = "中" * 67 + ".txt"
    with pytest.raises(HTTPException) as ei:
        _ingest(db_session, workspace, owner, filename=filename)
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST
    assert _file_count(db_session, workspace) == 0


def test_accepts_name_at_byte_limit(db_session, workspace, owner):
    filename = "a" * (MAX_FILENAME_BYTES - 4) + ".txt"  # exactly 200 bytes
    file_row, _task = _ingest(db_session, workspace, owner, filename=filename)
    assert file_row.uri == f"/{filename}"
    assert _file_count(db_session, workspace) == 1


def test_limit_applies_to_leaf_not_full_uri(db_session, workspace, owner):
    # A deep parent path does not count toward the per-component leaf budget.
    file_row, _task = _ingest(
        db_session, workspace, owner, path="/" + "/".join(["d"] * 40), filename="ok.txt"
    )
    assert file_row.name == "ok.txt"
