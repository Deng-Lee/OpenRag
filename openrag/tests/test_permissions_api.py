"""Tests for Permissions API"""

import pytest
from datetime import datetime
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.main import app
from openrag.api.deps import get_db, get_current_user
from openrag.models.base import Base
from openrag.models.user import User
from openrag.models.file import File
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.permission import FilePermission, EntityType, Permission
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
def other_user(db):
    """Create another test user"""
    user = User(
        username="otheruser",
        email="other@example.com",
        password_hash=hash_password("password123"),
        full_name="Other User",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_team(db, test_user):
    """Create test team"""
    team = Team(
        name="Test Team", description="Test team description", owner_id=test_user.id
    )
    db.add(team)
    db.commit()
    db.refresh(team)

    # Add owner as admin member
    member = TeamMember(team_id=team.id, user_id=test_user.id, role=TeamRole.ADMIN)
    db.add(member)
    db.commit()

    return team


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
def test_file(db, test_user, test_workspace):
    """Create test file"""
    file = File(
        uri="/test/file.txt",
        name="file.txt",
        owner_id=test_user.id,
        is_directory=False,
        size=1024,
        mime_type="text/plain",
        workspace_id=test_workspace.id,
    )
    db.add(file)
    db.commit()
    db.refresh(file)
    return file


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


def override_get_current_user_factory(user):
    """Factory to create override function for current user"""

    def override():
        return user

    return override


class TestListPermissions:
    """Test GET /files/{file_id}/permissions"""

    def test_list_permissions_as_owner(self, client, db, test_user, test_file):
        """Test listing permissions as file owner"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.get(f"/files/{test_file.id}/permissions")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0  # No permissions granted yet

    def test_list_permissions_with_existing_permissions(
        self, client, db, test_user, other_user, test_file
    ):
        """Test listing permissions when permissions exist"""
        # Grant permission to other user
        perm = FilePermission(
            file_id=test_file.id,
            entity_type=EntityType.USER,
            entity_id=other_user.id,
            permission=Permission.READ,
        )
        db.add(perm)
        db.commit()

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.get(f"/files/{test_file.id}/permissions")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["entity_type"] == "user"
        assert data[0]["entity_id"] == other_user.id
        assert data[0]["permission"] == "read"

    def test_list_permissions_as_non_owner(self, client, db, other_user, test_file):
        """Test listing permissions as non-owner (should fail)"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            other_user
        )

        response = client.get(f"/files/{test_file.id}/permissions")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_list_permissions_file_not_found(self, client, db, test_user):
        """Test listing permissions for non-existent file"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.get("/files/99999/permissions")

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestGrantPermission:
    """Test POST /files/{file_id}/permissions"""

    def test_grant_permission_to_user(
        self, client, db, test_user, other_user, test_file
    ):
        """Test granting permission to a user"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.post(
            f"/files/{test_file.id}/permissions",
            json={
                "entity_type": "user",
                "entity_id": other_user.id,
                "permission": "read",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["entity_type"] == "user"
        assert data["entity_id"] == other_user.id
        assert data["permission"] == "read"
        assert data["file_id"] == test_file.id

    def test_grant_permission_to_team(
        self, client, db, test_user, test_team, test_file
    ):
        """Test granting permission to a team"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.post(
            f"/files/{test_file.id}/permissions",
            json={
                "entity_type": "team",
                "entity_id": test_team.id,
                "permission": "write",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["entity_type"] == "team"
        assert data["entity_id"] == test_team.id
        assert data["permission"] == "write"

    def test_grant_permission_as_non_owner(self, client, db, other_user, test_file):
        """Test granting permission as non-owner (should fail)"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            other_user
        )

        response = client.post(
            f"/files/{test_file.id}/permissions",
            json={
                "entity_type": "user",
                "entity_id": other_user.id,
                "permission": "read",
            },
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_grant_permission_invalid_entity_type(
        self, client, db, test_user, test_file
    ):
        """Test granting permission with invalid entity type"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.post(
            f"/files/{test_file.id}/permissions",
            json={"entity_type": "invalid", "entity_id": 1, "permission": "read"},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_grant_permission_invalid_permission_level(
        self, client, db, test_user, other_user, test_file
    ):
        """Test granting permission with invalid permission level"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.post(
            f"/files/{test_file.id}/permissions",
            json={
                "entity_type": "user",
                "entity_id": other_user.id,
                "permission": "invalid",
            },
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_grant_permission_nonexistent_user(self, client, db, test_user, test_file):
        """Test granting permission to non-existent user"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.post(
            f"/files/{test_file.id}/permissions",
            json={"entity_type": "user", "entity_id": 99999, "permission": "read"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_grant_permission_duplicate(
        self, client, db, test_user, other_user, test_file
    ):
        """Test granting duplicate permission"""
        # Grant permission first time
        perm = FilePermission(
            file_id=test_file.id,
            entity_type=EntityType.USER,
            entity_id=other_user.id,
            permission=Permission.READ,
        )
        db.add(perm)
        db.commit()

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        # Try to grant again
        response = client.post(
            f"/files/{test_file.id}/permissions",
            json={
                "entity_type": "user",
                "entity_id": other_user.id,
                "permission": "write",
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestRevokePermission:
    """Test DELETE /files/{file_id}/permissions/{permission_id}"""

    def test_revoke_permission_success(
        self, client, db, test_user, other_user, test_file
    ):
        """Test revoking permission successfully"""
        # Grant permission first
        perm = FilePermission(
            file_id=test_file.id,
            entity_type=EntityType.USER,
            entity_id=other_user.id,
            permission=Permission.READ,
        )
        db.add(perm)
        db.commit()
        db.refresh(perm)

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.delete(f"/files/{test_file.id}/permissions/{perm.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["message"] == "Permission revoked successfully"

        # Verify permission is deleted
        deleted_perm = (
            db.query(FilePermission).filter(FilePermission.id == perm.id).first()
        )
        assert deleted_perm is None

    def test_revoke_permission_as_non_owner(
        self, client, db, test_user, other_user, test_file
    ):
        """Test revoking permission as non-owner (should fail)"""
        # Grant permission first
        perm = FilePermission(
            file_id=test_file.id,
            entity_type=EntityType.USER,
            entity_id=other_user.id,
            permission=Permission.READ,
        )
        db.add(perm)
        db.commit()
        db.refresh(perm)

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            other_user
        )

        response = client.delete(f"/files/{test_file.id}/permissions/{perm.id}")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_revoke_permission_not_found(self, client, db, test_user, test_file):
        """Test revoking non-existent permission"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.delete(f"/files/{test_file.id}/permissions/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_revoke_permission_file_not_found(self, client, db, test_user):
        """Test revoking permission for non-existent file"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        response = client.delete("/files/99999/permissions/1")

        assert response.status_code == status.HTTP_404_NOT_FOUND


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

    assert "roles" in data
    assert len(data["roles"]) == 1
    assert data["roles"][0]["role_code"] == "admin"

    assert "workspace_permissions" in data
    perms = data["workspace_permissions"]
    assert len(perms) == 2

    sources = [p["source"] for p in perms]
    assert "direct" in sources
    assert "role:Admin" in sources
