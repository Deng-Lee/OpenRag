"""Tests for workspace_file_tree helpers."""

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace
from openrag.services.workspace_file_tree import (
    CHILDREN_LIMIT,
    build_nested_tree,
    get_file_document_by_path,
    list_direct_children,
)


@pytest.fixture(scope="function")
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def owner(db_session: Session) -> User:
    u = User(
        username="wt_owner",
        email="wt@example.com",
        password_hash="h",
        full_name="W",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def workspace(db_session: Session, owner: User) -> Workspace:
    ws = Workspace(name="WT WS", slug="wt-ws", owner_id=owner.id)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _dir(db: Session, ws: Workspace, owner: User, uri: str, name: str) -> File:
    d = File(
        uri=uri,
        name=name,
        owner_id=owner.id,
        workspace_id=ws.id,
        is_directory=True,
        size=0,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _file(db: Session, ws: Workspace, owner: User, uri: str, name: str) -> File:
    f = File(
        uri=uri,
        name=name,
        owner_id=owner.id,
        workspace_id=ws.id,
        is_directory=False,
        size=10,
        mime_type="text/plain",
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def test_list_children_root(db_session: Session, workspace: Workspace, owner: User) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    _dir(db_session, workspace, owner, "/docs", "docs")
    _file(db_session, workspace, owner, "/readme.txt", "readme.txt")

    kids = list_direct_children(db_session, workspace.id, "/")
    uris = {f.uri for f in kids}
    assert uris == {"/docs", "/readme.txt"}


def test_list_children_nested(db_session: Session, workspace: Workspace, owner: User) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    _dir(db_session, workspace, owner, "/docs", "docs")
    _file(db_session, workspace, owner, "/docs/a.txt", "a.txt")
    _dir(db_session, workspace, owner, "/docs/sub", "sub")

    kids = list_direct_children(db_session, workspace.id, "/docs")
    assert {f.uri for f in kids} == {"/docs/a.txt", "/docs/sub"}


def test_list_children_missing_dir(db_session: Session, workspace: Workspace) -> None:
    with pytest.raises(HTTPException) as ei:
        list_direct_children(db_session, workspace.id, "/nope")
    assert ei.value.status_code == status.HTTP_404_NOT_FOUND


def test_get_file_document_ok(db_session: Session, workspace: Workspace, owner: User) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    f = _file(db_session, workspace, owner, "/x.pdf", "x.pdf")
    got = get_file_document_by_path(db_session, workspace.id, "/x.pdf")
    assert got.id == f.id


def test_get_file_document_directory_raises_400(
    db_session: Session, workspace: Workspace, owner: User
) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    _dir(db_session, workspace, owner, "/d", "d")
    with pytest.raises(HTTPException) as ei:
        get_file_document_by_path(db_session, workspace.id, "/d")
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST


def test_build_tree_root(db_session: Session, workspace: Workspace, owner: User) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    _dir(db_session, workspace, owner, "/docs", "docs")
    _file(db_session, workspace, owner, "/docs/f.txt", "f.txt")

    tree = build_nested_tree(db_session, workspace.id, "/")
    assert tree["path"] == "/"
    assert tree["kind"] == "dir"
    child_paths = {c["path"] for c in (tree.get("children") or [])}
    assert "/docs" in child_paths


def test_build_tree_under_prefix(db_session: Session, workspace: Workspace, owner: User) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    _dir(db_session, workspace, owner, "/docs", "docs")
    _file(db_session, workspace, owner, "/docs/f.txt", "f.txt")

    tree = build_nested_tree(db_session, workspace.id, "/docs")
    assert tree["path"] == "/docs"
    assert tree["kind"] == "dir"
    names = {c["path"] for c in tree["children"]}
    assert "/docs/f.txt" in names


def test_build_tree_prefix_not_dir_raises_404(
    db_session: Session, workspace: Workspace, owner: User
) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    _file(db_session, workspace, owner, "/only.txt", "only.txt")
    with pytest.raises(HTTPException) as ei:
        build_nested_tree(db_session, workspace.id, "/only.txt")
    assert ei.value.status_code == status.HTTP_404_NOT_FOUND


def test_children_limit_raises(db_session: Session, workspace: Workspace, owner: User) -> None:
    _dir(db_session, workspace, owner, "/", "root")
    _dir(db_session, workspace, owner, "/many", "many")
    for i in range(CHILDREN_LIMIT + 1):
        _file(db_session, workspace, owner, f"/many/f{i}.txt", f"f{i}.txt")
    with pytest.raises(HTTPException) as ei:
        list_direct_children(db_session, workspace.id, "/many")
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST
