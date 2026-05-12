"""Tests for workspace name resolution and service-token permission checks."""

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, User, Workspace
from openrag.services.service_token_service import (
    ServiceTokenContext,
    TokenWorkspaceBinding,
    assert_token_workspace_permission,
    require_workspace_for_name,
)


@pytest.fixture(scope="function")
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def owner(db_session: Session) -> User:
    user = User(
        username="owner2",
        email="owner2@example.com",
        password_hash="hash",
        full_name="Owner",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def workspace(db_session: Session, owner: User) -> Workspace:
    ws = Workspace(
        name="Unique WS Name",
        slug="unique-ws-name",
        owner_id=owner.id,
    )
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


@pytest.fixture
def workspace_b(db_session: Session, owner: User) -> Workspace:
    ws = Workspace(
        name="Unique WS Name B",
        slug="unique-ws-name-b",
        owner_id=owner.id,
    )
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def test_require_workspace_for_name_ok(db_session: Session, workspace: Workspace) -> None:
    ws = require_workspace_for_name(db_session, "Unique WS Name")
    assert ws.id == workspace.id


def test_require_workspace_for_name_strips_whitespace(
    db_session: Session, workspace: Workspace
) -> None:
    ws = require_workspace_for_name(db_session, "  Unique WS Name  ")
    assert ws.id == workspace.id


def test_require_workspace_for_name_unknown_raises_404(db_session: Session) -> None:
    with pytest.raises(HTTPException) as ei:
        require_workspace_for_name(db_session, "no-such-workspace")
    assert ei.value.status_code == status.HTTP_404_NOT_FOUND


def test_require_workspace_for_name_empty_raises_404(db_session: Session) -> None:
    with pytest.raises(HTTPException) as ei:
        require_workspace_for_name(db_session, "   ")
    assert ei.value.status_code == status.HTTP_404_NOT_FOUND


def test_assert_read_allows_read_grant(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="read")],
    )
    assert_token_workspace_permission(ctx, workspace.id, "read")


def test_assert_read_allows_write_grant(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="write")],
    )
    assert_token_workspace_permission(ctx, workspace.id, "read")


def test_assert_read_denies_wrong_workspace(workspace: Workspace, workspace_b: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="read")],
    )
    with pytest.raises(HTTPException) as ei:
        assert_token_workspace_permission(ctx, workspace_b.id, "read")
    assert ei.value.status_code == status.HTTP_403_FORBIDDEN
    assert "Token not authorized for this workspace" in ei.value.detail


def test_assert_write_allows_write_only(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="write")],
    )
    assert_token_workspace_permission(ctx, workspace.id, "write")


def test_assert_write_denies_read_grant(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="read")],
    )
    with pytest.raises(HTTPException) as ei:
        assert_token_workspace_permission(ctx, workspace.id, "write")
    assert ei.value.status_code == status.HTTP_403_FORBIDDEN
    assert "Token permission insufficient" in ei.value.detail


def test_assert_multi_workspace_binding_grants_access(
    workspace: Workspace, workspace_b: Workspace
) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[
            TokenWorkspaceBinding(workspace_id=workspace.id, permission="write"),
            TokenWorkspaceBinding(workspace_id=workspace_b.id, permission="read"),
        ],
    )
    assert_token_workspace_permission(ctx, workspace.id, "write")
    assert_token_workspace_permission(ctx, workspace_b.id, "read")
    with pytest.raises(HTTPException):
        assert_token_workspace_permission(ctx, workspace_b.id, "write")