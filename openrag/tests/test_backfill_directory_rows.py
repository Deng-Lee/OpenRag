"""Tests for scripts/backfill_directory_rows.py.

Focus on the old-version (no ``ensure_directory_path``) inline fallback, since
that is the path that runs against older deployments (e.g. 1.1.x).
"""

import importlib.util
import pathlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace

_SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "scripts"
    / "backfill_directory_rows.py"
)


def _load_bf():
    spec = importlib.util.spec_from_file_location("backfill_directory_rows", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load_bf()


@pytest.fixture(scope="function")
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def owner(db_session):
    u = User(
        username="bf_owner",
        email="bf@example.com",
        password_hash="h",
        full_name="B",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def workspace(db_session, owner):
    ws = Workspace(name="BF WS", slug="bf-ws", owner_id=owner.id)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _file(db, ws, owner, uri):
    db.add(
        File(
            uri=uri,
            name=uri.rsplit("/", 1)[-1] or uri,
            owner_id=owner.id,
            workspace_id=ws.id,
            is_directory=False,
            size=10,
        )
    )
    db.commit()


def _dir_uris(db, ws):
    return {
        r.uri
        for r in db.query(File).filter(
            File.workspace_id == ws.id, File.is_directory.is_(True)
        )
    }


def test_inline_fallback_creates_ancestor_dirs(db_session, workspace, owner, monkeypatch):
    # Simulate an old deployment without ensure_directory_path -> inline path.
    monkeypatch.setattr(bf, "ensure_directory_path", None)
    _file(db_session, workspace, owner, "/feishu/wiki/Docker.md")
    _file(db_session, workspace, owner, "/feishu/top.md")

    r = bf.process_workspace(db_session, workspace, apply=True)

    dirs = _dir_uris(db_session, workspace)
    assert "/feishu" in dirs
    assert "/feishu/wiki" in dirs
    assert r["created"] == 2


def test_inline_dry_run_creates_nothing(db_session, workspace, owner, monkeypatch):
    monkeypatch.setattr(bf, "ensure_directory_path", None)
    _file(db_session, workspace, owner, "/a/b/c.md")

    r = bf.process_workspace(db_session, workspace, apply=False)

    assert _dir_uris(db_session, workspace) == set()
    assert r["missing"] == 2
    assert r["created"] == 0


def test_inline_idempotent(db_session, workspace, owner, monkeypatch):
    monkeypatch.setattr(bf, "ensure_directory_path", None)
    _file(db_session, workspace, owner, "/a/b/c.md")

    bf.process_workspace(db_session, workspace, apply=True)
    second = bf.process_workspace(db_session, workspace, apply=True)

    assert second["created"] == 0
    for uri in ("/a", "/a/b"):
        count = (
            db_session.query(File)
            .filter(File.workspace_id == workspace.id, File.uri == uri)
            .count()
        )
        assert count == 1


def test_inline_conflict_is_skipped(db_session, workspace, owner, monkeypatch):
    monkeypatch.setattr(bf, "ensure_directory_path", None)
    # "/a" already exists as a FILE; "/a/b.md" would need "/a" as a directory.
    _file(db_session, workspace, owner, "/a")
    _file(db_session, workspace, owner, "/a/b.md")

    r = bf.process_workspace(db_session, workspace, apply=True)

    assert r["conflicts"] == 1
    assert "/a" not in _dir_uris(db_session, workspace)


def test_with_ensure_directory_path_path(db_session, workspace, owner):
    # When ensure_directory_path is available (new builds), it is used.
    if bf.ensure_directory_path is None:
        pytest.skip("ensure_directory_path not available in this build")
    _file(db_session, workspace, owner, "/x/y/z.md")

    bf.process_workspace(db_session, workspace, apply=True)

    dirs = _dir_uris(db_session, workspace)
    assert "/x" in dirs
    assert "/x/y" in dirs
