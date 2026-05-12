import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base
from openrag.services.workspace_service import WorkspaceService
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User
from openrag.models.role import Role, RoleWorkspacePermission, UserRole


@pytest.fixture(scope="function")
def db():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


def test_check_user_permission_direct_grant(db):
    user = User(
        username="user", email="user@test.com", password_hash="hash", full_name="User"
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="read")
    db.add(member)
    db.commit()

    service = WorkspaceService(db)
    assert service.check_user_permission(workspace.id, user.id, "read") is True
    assert service.check_user_permission(workspace.id, user.id, "write") is False


def test_check_user_permission_role_grant(db):
    user = User(
        username="user2", email="user2@test.com", password_hash="hash", full_name="User"
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    role = Role(name="Admin", role_code="admin", is_active=True)
    db.add_all([workspace, role])
    db.commit()

    db.add(
        RoleWorkspacePermission(
            role_id=role.id, workspace_id=workspace.id, permission="write"
        )
    )
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()

    service = WorkspaceService(db)
    assert service.check_user_permission(workspace.id, user.id, "write") is True


def test_check_user_permission_union_highest(db):
    user = User(
        username="user3", email="user3@test.com", password_hash="hash", full_name="User"
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    role = Role(name="Admin", role_code="admin", is_active=True)
    db.add_all([workspace, role])
    db.commit()

    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role="read"))
    db.add(
        RoleWorkspacePermission(
            role_id=role.id, workspace_id=workspace.id, permission="write"
        )
    )
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()

    service = WorkspaceService(db)
    assert service.check_user_permission(workspace.id, user.id, "write") is True


def test_check_user_permission_inactive_role(db):
    user = User(
        username="user4", email="user4@test.com", password_hash="hash", full_name="User"
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    role = Role(name="Admin", role_code="admin", is_active=False)
    db.add_all([workspace, role])
    db.commit()

    db.add(
        RoleWorkspacePermission(
            role_id=role.id, workspace_id=workspace.id, permission="write"
        )
    )
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()

    service = WorkspaceService(db)
    assert service.check_user_permission(workspace.id, user.id, "write") is False
    assert service.check_user_permission(workspace.id, user.id, "read") is False
