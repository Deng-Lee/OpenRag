"""Plan B: ``ingest_new_file`` must create ancestor directory rows.

Folder upload stores files with deep URIs but historically created no
``is_directory`` rows, so the directory tree (web lazy-load + service API)
could not show the folders. ``ingest_new_file`` (the single chokepoint for both
the web ``/files/upload`` and the service-token upload) must, in the lenient
``require_parent_dir=False`` path, create the ancestor directory rows.
"""

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace
from openrag.services import file_ingest
from openrag.services.file_ingest import ingest_new_file


class _FakeMinio:
    def put_file(self, *args, **kwargs):
        return None


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
    monkeypatch.setattr(file_ingest, "MinioStorage", _FakeMinio)


@pytest.fixture
def owner(db_session: Session) -> User:
    u = User(
        username="ing_owner",
        email="ing@example.com",
        password_hash="h",
        full_name="I",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def workspace(db_session: Session, owner: User) -> Workspace:
    ws = Workspace(name="Ing WS", slug="ing-ws", owner_id=owner.id)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _dir_uris(db: Session, ws: Workspace) -> set:
    return {
        r.uri
        for r in db.query(File).filter(
            File.workspace_id == ws.id, File.is_directory.is_(True)
        )
    }


def _ingest(db, ws, owner, *, path, filename):
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


def test_ingest_creates_ancestor_directory_rows(db_session, workspace, owner):
    file_row, _task = _ingest(db_session, workspace, owner, path="/a/b", filename="c.txt")

    assert file_row.uri == "/a/b/c.txt"
    assert file_row.is_directory is False

    a = (
        db_session.query(File)
        .filter(File.workspace_id == workspace.id, File.uri == "/a")
        .one()
    )
    b = (
        db_session.query(File)
        .filter(File.workspace_id == workspace.id, File.uri == "/a/b")
        .one()
    )
    assert a.is_directory is True
    assert b.is_directory is True


def test_ingest_creating_dirs_is_idempotent(db_session, workspace, owner):
    _ingest(db_session, workspace, owner, path="/a/b", filename="c1.txt")
    dirs_after_first = _dir_uris(db_session, workspace)

    _ingest(db_session, workspace, owner, path="/a/b", filename="c2.txt")
    dirs_after_second = _dir_uris(db_session, workspace)

    assert dirs_after_first == dirs_after_second
    for uri in ("/a", "/a/b"):
        count = (
            db_session.query(File)
            .filter(File.workspace_id == workspace.id, File.uri == uri)
            .count()
        )
        assert count == 1, f"expected exactly one row for {uri}, got {count}"


def test_ingest_root_file_creates_no_nested_dirs(db_session, workspace, owner):
    file_row, _ = _ingest(db_session, workspace, owner, path="/", filename="top.txt")

    assert file_row.uri == "/top.txt"
    nested = {u for u in _dir_uris(db_session, workspace) if u != "/"}
    assert nested == set()


def test_ingest_strict_mode_missing_parent_still_rejected(db_session, workspace, owner):
    # Regression guard: strict mode (require_parent_dir=True) must be unaffected by
    # the lenient-path auto-create. Missing parent still raises 400 (existing behavior).
    with pytest.raises(HTTPException) as ei:
        ingest_new_file(
            db_session,
            workspace,
            owner.id,
            parent_logical_path="/missing",
            upload_filename="x.txt",
            file_content=b"x",
            content_type="text/plain",
            require_parent_dir=True,
        )
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST
