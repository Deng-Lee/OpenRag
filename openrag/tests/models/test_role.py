import pytest
from sqlalchemy import select
from openrag.models.role import Role, RoleWorkspacePermission, UserRole
from openrag.models.user import User
from openrag.models.workspace import Workspace


def test_role_creation(db_session):
    role = Role(name="Admin", role_code="admin_role", is_active=True)
    db_session.add(role)
    db_session.commit()

    saved_role = db_session.scalar(select(Role).where(Role.role_code == "admin_role"))
    assert saved_role is not None
    assert saved_role.name == "Admin"
    assert saved_role.is_active is True


def test_role_workspace_permission(db_session):
    user = User(
        username="test", email="test@test.com", password_hash="hash", full_name="Test"
    )
    db_session.add(user)
    db_session.commit()

    workspace = Workspace(name="Test WS", slug="test-ws", owner_id=user.id)
    role = Role(name="Viewer", role_code="viewer_role")
    db_session.add_all([workspace, role])
    db_session.commit()

    perm = RoleWorkspacePermission(
        role_id=role.id, workspace_id=workspace.id, permission="read"
    )
    db_session.add(perm)
    db_session.commit()

    saved_perm = db_session.scalar(
        select(RoleWorkspacePermission).where(
            RoleWorkspacePermission.role_id == role.id
        )
    )
    assert saved_perm.permission == "read"
    assert saved_perm.workspace.name == "Test WS"


def test_user_role_assignment(db_session):
    user = User(
        username="test2",
        email="test2@test.com",
        password_hash="hash",
        full_name="Test 2",
    )
    role = Role(name="Editor", role_code="editor_role")
    db_session.add_all([user, role])
    db_session.commit()

    user_role = UserRole(user_id=user.id, role_id=role.id)
    db_session.add(user_role)
    db_session.commit()

    assert len(user.roles) == 1
    assert user.roles[0].role_code == "editor_role"
