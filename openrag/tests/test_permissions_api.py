"""Tests for User Permissions API"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.main import app
from openrag.api.deps import get_db, get_current_user
from openrag.models.base import Base
from openrag.models.user import User
from openrag.security import hash_password


# Test database setup
TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    """Override database dependency for testing"""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="function", autouse=True)
def setup_dependencies():
    """Setup and teardown dependency overrides"""
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()


@pytest.fixture(scope="function")
def db():
    """Create test database and tables"""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def test_user(db):
    """Create test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=hash_password("password123"),
        full_name="Test User",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_workspace(db, test_user):
    from openrag.models.workspace import Workspace

    workspace = Workspace(
        name="Test Workspace", slug="test-workspace", owner_id=test_user.id
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


def override_get_current_user_factory(user):
    """Factory to create override function for current user"""

    def override():
        return user

    return override


def test_get_user_permission_details(client, db, test_user, test_workspace):
    from openrag.models.workspace import WorkspaceMember
    from openrag.models.role import Role, RoleWorkspacePermission, UserRole

    db.add(
        WorkspaceMember(
            user_id=test_user.id, workspace_id=test_workspace.id, role="read"
        )
    )

    role = Role(name="Admin", role_code="admin", is_active=True)
    db.add(role)
    db.commit()

    db.add(
        RoleWorkspacePermission(
            role_id=role.id, workspace_id=test_workspace.id, permission="write"
        )
    )
    db.add(UserRole(user_id=test_user.id, role_id=role.id))
    db.commit()

    app.dependency_overrides[get_current_user] = override_get_current_user_factory(
        test_user
    )

    response = client.get(f"/users/{test_user.id}/permissions/details")
    assert response.status_code == 200
    data = response.json()

    assert data["user_id"] == test_user.id
    assert "roles" in data
    assert len(data["roles"]) == 1
    assert data["roles"][0]["role_code"] == "admin"

    assert "workspace_permissions" in data
    perms = data["workspace_permissions"]
    assert len(perms) == 2

    sources = [p["source"] for p in perms]
    assert "direct" in sources
    assert "role:Admin" in sources
