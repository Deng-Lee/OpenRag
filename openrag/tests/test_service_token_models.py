"""Tests for ServiceToken ORM model."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, ServiceToken, ServiceTokenWorkspace, User, Workspace


@pytest.fixture(scope="function")
def db_session() -> Session:
    from sqlalchemy import event

    engine = create_engine("sqlite:///:memory:", echo=False)

    # Enable SQLite foreign key enforcement so ON DELETE CASCADE works
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
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
        name="Unique Workspace Alpha",
        slug="unique-workspace-alpha",
        owner_id=owner.id,
    )
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def test_service_token_created_by_relationship(
    db_session: Session, owner: User
) -> None:
    token = ServiceToken(
        secret="opaque-secret-value-xxxxxxxxxxxxxxxx",
        name="CI token",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    assert token in owner.created_service_tokens


def test_service_token_secret_unique(db_session: Session, owner: User) -> None:
    db_session.add_all(
        [
            ServiceToken(secret="same-secret", created_by_user_id=owner.id),
            ServiceToken(secret="same-secret", created_by_user_id=owner.id),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_workspace_name_unique(db_session: Session, owner: User) -> None:
    db_session.add_all(
        [
            Workspace(name="DupName", slug="slug-a", owner_id=owner.id),
            Workspace(name="DupName", slug="slug-b", owner_id=owner.id),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_revoked_at_timezone_aware(db_session: Session, owner: User) -> None:
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    token = ServiceToken(
        secret="revoked-secret",
        created_by_user_id=owner.id,
        revoked_at=when,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)
    assert token.revoked_at is not None
    assert token.revoked_at.replace(tzinfo=None) == when.replace(tzinfo=None)


def test_service_token_workspace_binding(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    token = ServiceToken(
        secret="sk-binding-test",
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
    db_session.refresh(binding)

    assert binding.token_id == token.id
    assert binding.workspace_id == workspace.id
    assert binding.permission == "read"


def test_service_token_workspace_unique_constraint(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    token = ServiceToken(
        secret="sk-unique-binding",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    b1 = ServiceTokenWorkspace(token_id=token.id, workspace_id=workspace.id, permission="read")
    b2 = ServiceTokenWorkspace(token_id=token.id, workspace_id=workspace.id, permission="write")
    db_session.add_all([b1, b2])
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_service_token_workspace_cascade_delete(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    token = ServiceToken(
        secret="sk-cascade-delete",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    binding = ServiceTokenWorkspace(
        token_id=token.id,
        workspace_id=workspace.id,
        permission="write",
    )
    db_session.add(binding)
    db_session.commit()

    db_session.delete(token)
    db_session.commit()

    assert db_session.query(ServiceTokenWorkspace).filter_by(token_id=token.id).count() == 0