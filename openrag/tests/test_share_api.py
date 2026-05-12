"""Tests for Share API"""

import pytest
from datetime import datetime, timedelta, timezone
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
from openrag.models.share import ShareLink
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
    try:
        db = TestingSessionLocal()
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
        is_active=True
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
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_file(db, test_user):
    """Create test file"""
    file = File(
        uri="/test/file.txt",
        name="file.txt",
        owner_id=test_user.id,
        is_directory=False,
        size=1024,
        mime_type="text/plain"
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


class TestCreateShareLink:
    """Test POST /share/links"""

    def test_create_share_link_basic(self, client, db, test_user, test_file):
        """Test creating basic share link without password or expiration"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.post(
            "/share/links",
            json={
                "file_id": test_file.id
            }
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["file_id"] == test_file.id
        assert "token" in data
        assert len(data["token"]) > 0
        assert data["password_protected"] is False
        assert data["expires_at"] is None
        assert data["access_count"] == 0

    def test_create_share_link_with_password(self, client, db, test_user, test_file):
        """Test creating share link with password protection"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.post(
            "/share/links",
            json={
                "file_id": test_file.id,
                "password": "secret123"
            }
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["password_protected"] is True

    def test_create_share_link_with_expiration(self, client, db, test_user, test_file):
        """Test creating share link with expiration"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

        response = client.post(
            "/share/links",
            json={
                "file_id": test_file.id,
                "expires_at": expires_at
            }
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["expires_at"] is not None

    def test_create_share_link_with_max_access(self, client, db, test_user, test_file):
        """Test creating share link with max access count"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.post(
            "/share/links",
            json={
                "file_id": test_file.id,
                "max_access_count": 10
            }
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["max_access_count"] == 10

    def test_create_share_link_file_not_found(self, client, db, test_user):
        """Test creating share link for non-existent file"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.post(
            "/share/links",
            json={
                "file_id": 99999
            }
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_share_link_no_permission(self, client, db, other_user, test_file):
        """Test creating share link without permission"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(other_user)

        response = client.post(
            "/share/links",
            json={
                "file_id": test_file.id
            }
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestListShareLinks:
    """Test GET /share/links"""

    def test_list_share_links_empty(self, client, db, test_user):
        """Test listing share links when none exist"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get("/share/links")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_list_share_links_with_links(self, client, db, test_user, test_file):
        """Test listing share links when they exist"""
        # Create share link
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id)

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get("/share/links")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["token"] == link.token


class TestGetShareLink:
    """Test GET /share/links/{link_id}"""

    def test_get_share_link_success(self, client, db, test_user, test_file):
        """Test getting share link details"""
        # Create share link
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id)

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get(f"/share/links/{link.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == link.id
        assert data["token"] == link.token

    def test_get_share_link_not_found(self, client, db, test_user):
        """Test getting non-existent share link"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get("/share/links/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_share_link_unauthorized(self, client, db, test_user, other_user, test_file):
        """Test getting share link created by another user"""
        # Create share link as test_user
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id)

        # Try to access as other_user
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(other_user)

        response = client.get(f"/share/links/{link.id}")

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestDeleteShareLink:
    """Test DELETE /share/links/{link_id}"""

    def test_delete_share_link_success(self, client, db, test_user, test_file):
        """Test deleting share link successfully"""
        # Create share link
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id)

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.delete(f"/share/links/{link.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["message"] == "Share link deleted successfully"

        # Verify link is deleted
        deleted_link = db.query(ShareLink).filter(ShareLink.id == link.id).first()
        assert deleted_link is None

    def test_delete_share_link_not_found(self, client, db, test_user):
        """Test deleting non-existent share link"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.delete("/share/links/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_share_link_unauthorized(self, client, db, test_user, other_user, test_file):
        """Test deleting share link created by another user"""
        # Create share link as test_user
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id)

        # Try to delete as other_user
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(other_user)

        response = client.delete(f"/share/links/{link.id}")

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestAccessShareLink:
    """Test GET /share/{token}"""

    def test_access_share_link_success(self, client, db, test_user, test_file):
        """Test accessing file via share link"""
        # Create share link
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id)

        response = client.get(f"/share/{link.token}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["file"]["id"] == test_file.id
        assert data["file"]["name"] == test_file.name

        # Verify access count incremented
        db.refresh(link)
        assert link.access_count == 1

    def test_access_share_link_with_password_correct(self, client, db, test_user, test_file):
        """Test accessing password-protected share link with correct password"""
        # Create share link with password
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id, password="secret123")

        response = client.get(
            f"/share/{link.token}",
            params={"password": "secret123"}
        )

        assert response.status_code == status.HTTP_200_OK

    def test_access_share_link_with_password_incorrect(self, client, db, test_user, test_file):
        """Test accessing password-protected share link with wrong password"""
        # Create share link with password
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id, password="secret123")

        response = client.get(
            f"/share/{link.token}",
            params={"password": "wrongpassword"}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_access_share_link_with_password_missing(self, client, db, test_user, test_file):
        """Test accessing password-protected share link without password"""
        # Create share link with password
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id, password="secret123")

        response = client.get(f"/share/{link.token}")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_access_share_link_expired(self, client, db, test_user, test_file):
        """Test accessing expired share link"""
        # Create share link with past expiration
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        link = manager.create_share_link(test_file.id, test_user.id, expires_at=expires_at)

        response = client.get(f"/share/{link.token}")

        assert response.status_code == status.HTTP_410_GONE

    def test_access_share_link_max_access_reached(self, client, db, test_user, test_file):
        """Test accessing share link when max access count reached"""
        # Create share link with max access count
        from openrag.services.share_manager import ShareLinkManager
        manager = ShareLinkManager(db)
        link = manager.create_share_link(test_file.id, test_user.id, max_access_count=1)

        # Access once (should succeed)
        response1 = client.get(f"/share/{link.token}")
        assert response1.status_code == status.HTTP_200_OK

        # Access again (should fail)
        response2 = client.get(f"/share/{link.token}")
        assert response2.status_code == status.HTTP_410_GONE

    def test_access_share_link_not_found(self, client, db):
        """Test accessing non-existent share link"""
        response = client.get("/share/invalid_token_12345")

        assert response.status_code == status.HTTP_404_NOT_FOUND
