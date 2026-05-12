"""Tests for service token resolution (header → context) and related deps."""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, ServiceToken, ServiceTokenWorkspace, User, Workspace
from openrag.services.service_token_service import (
    ServiceTokenContext,
    TokenWorkspaceBinding,
    resolve_service_token_context,
)


@pytest.fixture(scope="function")
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    from sqlalchemy import event

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
        username="owner",
        email="owner@example.com",
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
        name="Svc Token WS",
        slug="svc-token-ws",
        owner_id=owner.id,
    )
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def test_resolve_valid_token_returns_binding_context(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    token = ServiceToken(
        secret="sk-abc",
        name="api",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    binding = ServiceTokenWorkspace(
        token_id=token.id,
        workspace_id=workspace.id,
        permission="read",
    )
    db_session.add(binding)
    db_session.commit()

    ctx = resolve_service_token_context(db_session, "sk-abc")
    assert isinstance(ctx, ServiceTokenContext)
    assert ctx.token_id == token.id
    assert len(ctx.bindings) == 1
    assert ctx.bindings[0].workspace_id == workspace.id
    assert ctx.bindings[0].permission == "read"


def test_resolve_multi_workspace_token(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    ws2 = Workspace(name="WS2", slug="ws2", owner_id=owner.id)
    db_session.add(ws2)
    db_session.commit()
    db_session.refresh(ws2)

    token = ServiceToken(
        secret="sk-multi",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    db_session.add_all([
        ServiceTokenWorkspace(token_id=token.id, workspace_id=workspace.id, permission="write"),
        ServiceTokenWorkspace(token_id=token.id, workspace_id=ws2.id, permission="read"),
    ])
    db_session.commit()

    ctx = resolve_service_token_context(db_session, "sk-multi")
    assert len(ctx.bindings) == 2
    ws_ids = {b.workspace_id for b in ctx.bindings}
    assert ws_ids == {workspace.id, ws2.id}


def test_resolve_token_no_bindings_raises_403(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    token = ServiceToken(
        secret="sk-no-bindings",
        name="no-bindings",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    # Deliberately NOT creating any ServiceTokenWorkspace binding

    with pytest.raises(HTTPException) as exc:
        resolve_service_token_context(db_session, "sk-no-bindings")
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc.value.detail == "Token has no workspace bindings"


def test_resolve_revoked_token_raises_401(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    token = ServiceToken(
        secret="sk-revoked",
        created_by_user_id=owner.id,
        revoked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(token)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_service_token_context(db_session, "sk-revoked")
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Invalid or missing service token"


def test_resolve_wrong_secret_raises_401(db_session: Session, owner: User, workspace: Workspace) -> None:
    token = ServiceToken(
        secret="sk-real",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_service_token_context(db_session, "sk-other")
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_resolve_missing_or_bad_prefix_raises_401(db_session: Session, owner: User, workspace: Workspace) -> None:
    token = ServiceToken(
        secret="sk-x",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()

    for raw in (None, "", "   ", "not-sk", "bearer sk-x"):
        with pytest.raises(HTTPException) as exc:
            resolve_service_token_context(db_session, raw)
        assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED