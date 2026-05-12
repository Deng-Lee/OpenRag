"""Tests for share link management service"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models.base import Base
from openrag.models.file import File
from openrag.models.share import ShareLink
from openrag.models.user import User
from openrag.security import hash_password
from openrag.services.share_manager import ShareLinkManager


@pytest.fixture
def engine():
    """Create in-memory SQLite database engine"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session"""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def share_manager(session):
    """Create ShareLinkManager instance"""
    return ShareLinkManager(session)


@pytest.fixture
def test_user(session):
    """Create a test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=hash_password("password123"),
        full_name="Test User"
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture
def test_file(session, test_user):
    """Create a test file"""
    file = File(
        uri="/test/file.txt",
        name="file.txt",
        owner_id=test_user.id,
        size=1024,
        mime_type="text/plain"
    )
    session.add(file)
    session.commit()
    session.refresh(file)
    return file


class TestCreateShareLink:
    """Test share link creation"""

    def test_create_share_link_basic(self, share_manager, test_file, test_user):
        """Test creating a basic share link without restrictions"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )

        assert share_link.id is not None
        assert share_link.file_id == test_file.id
        assert share_link.token is not None
        assert len(share_link.token) > 0
        assert share_link.password_hash is None
        assert share_link.expires_at is None
        assert share_link.max_access_count is None
        assert share_link.access_count == 0
        assert share_link.created_by == test_user.id

    def test_create_share_link_with_password(self, share_manager, test_file, test_user):
        """Test creating a share link with password protection"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            password="secret123"
        )

        assert share_link.password_hash is not None
        assert share_link.password_hash != "secret123"  # Should be hashed

    def test_create_share_link_with_expiration(self, share_manager, test_file, test_user):
        """Test creating a share link with expiration time"""
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            expires_at=expires_at
        )

        assert share_link.expires_at is not None
        # Handle timezone-aware vs naive datetime comparison
        stored_expires = share_link.expires_at
        if stored_expires.tzinfo is None:
            # SQLite stores as naive, so compare with naive datetime
            expires_at_naive = expires_at.replace(tzinfo=None)
            assert abs((stored_expires - expires_at_naive).total_seconds()) < 1
        else:
            assert abs((stored_expires - expires_at).total_seconds()) < 1

    def test_create_share_link_with_max_access(self, share_manager, test_file, test_user):
        """Test creating a share link with max access count"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            max_access_count=10
        )

        assert share_link.max_access_count == 10

    def test_create_share_link_invalid_file(self, share_manager, test_user):
        """Test creating a share link for non-existent file raises error"""
        with pytest.raises(ValueError, match="File with id 99999 not found"):
            share_manager.create_share_link(
                file_id=99999,
                created_by=test_user.id
            )


class TestGetShareLink:
    """Test getting share links"""

    def test_get_share_link(self, share_manager, test_file, test_user):
        """Test getting a share link by token"""
        created_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )

        retrieved_link = share_manager.get_share_link(created_link.token)

        assert retrieved_link is not None
        assert retrieved_link.id == created_link.id
        assert retrieved_link.token == created_link.token

    def test_get_share_link_not_found(self, share_manager):
        """Test getting a non-existent share link returns None"""
        result = share_manager.get_share_link("nonexistent_token")
        assert result is None


class TestVerifyShareLink:
    """Test share link verification"""

    def test_verify_share_link_success(self, share_manager, test_file, test_user):
        """Test verifying a valid share link"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )

        result, error = share_manager.verify_share_link(share_link.token)

        assert result is not None
        assert error is None
        assert result.id == share_link.id

    def test_verify_share_link_not_found(self, share_manager):
        """Test verifying a non-existent share link returns error"""
        result, error = share_manager.verify_share_link("nonexistent_token")

        assert result is None
        assert error == "not_found"

    def test_verify_share_link_expired(self, share_manager, test_file, test_user):
        """Test verifying an expired share link returns error"""
        expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            expires_at=expires_at
        )

        result, error = share_manager.verify_share_link(share_link.token)

        assert result is None
        assert error == "expired"

    def test_verify_share_link_max_access_reached(self, share_manager, test_file, test_user):
        """Test verifying a share link that reached max access count"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            max_access_count=2
        )

        # Access twice to reach the limit
        share_manager.access_share_link(share_link.token)
        share_manager.access_share_link(share_link.token)

        # Third verification should fail
        result, error = share_manager.verify_share_link(share_link.token)

        assert result is None
        assert error == "max_access_reached"

    def test_verify_share_link_invalid_password(self, share_manager, test_file, test_user):
        """Test verifying a share link with wrong password"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            password="correct_password"
        )

        result, error = share_manager.verify_share_link(
            share_link.token,
            password="wrong_password"
        )

        assert result is None
        assert error == "invalid_password"

    def test_verify_share_link_correct_password(self, share_manager, test_file, test_user):
        """Test verifying a share link with correct password"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            password="correct_password"
        )

        result, error = share_manager.verify_share_link(
            share_link.token,
            password="correct_password"
        )

        assert result is not None
        assert error is None


class TestAccessShareLink:
    """Test accessing share links"""

    def test_access_share_link_success(self, share_manager, test_file, test_user):
        """Test accessing a valid share link"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )

        file, error = share_manager.access_share_link(share_link.token)

        assert file is not None
        assert error is None
        assert file.id == test_file.id

    def test_access_share_link_increments_count(self, share_manager, test_file, test_user, session):
        """Test that accessing a share link increments the access count"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )

        initial_count = share_link.access_count

        share_manager.access_share_link(share_link.token)
        session.refresh(share_link)

        assert share_link.access_count == initial_count + 1

        share_manager.access_share_link(share_link.token)
        session.refresh(share_link)

        assert share_link.access_count == initial_count + 2

    def test_access_share_link_expired(self, share_manager, test_file, test_user):
        """Test accessing an expired share link returns error"""
        expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            expires_at=expires_at
        )

        file, error = share_manager.access_share_link(share_link.token)

        assert file is None
        assert error == "expired"


class TestRevokeShareLink:
    """Test revoking share links"""

    def test_revoke_share_link(self, share_manager, test_file, test_user):
        """Test revoking a share link"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )

        result = share_manager.revoke_share_link(share_link.token)

        assert result is True

        # Verify it's deleted
        retrieved = share_manager.get_share_link(share_link.token)
        assert retrieved is None

    def test_revoke_share_link_not_found(self, share_manager):
        """Test revoking a non-existent share link returns False"""
        result = share_manager.revoke_share_link("nonexistent_token")
        assert result is False


class TestListFileShareLinks:
    """Test listing share links for a file"""

    def test_list_file_share_links(self, share_manager, test_file, test_user):
        """Test listing all share links for a file"""
        # Create multiple share links
        link1 = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )
        link2 = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id,
            password="password"
        )

        links = share_manager.list_file_share_links(test_file.id)

        assert len(links) == 2
        tokens = [link.token for link in links]
        assert link1.token in tokens
        assert link2.token in tokens


class TestUpdateShareLink:
    """Test updating share links"""

    def test_update_share_link(self, share_manager, test_file, test_user):
        """Test updating share link settings"""
        share_link = share_manager.create_share_link(
            file_id=test_file.id,
            created_by=test_user.id
        )

        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        updated = share_manager.update_share_link(
            token=share_link.token,
            password="new_password",
            expires_at=expires_at,
            max_access_count=5
        )

        assert updated is not None
        assert updated.password_hash is not None
        assert updated.expires_at is not None
        assert updated.max_access_count == 5

    def test_update_share_link_not_found(self, share_manager):
        """Test updating a non-existent share link returns None"""
        result = share_manager.update_share_link(
            token="nonexistent_token",
            max_access_count=10
        )
        assert result is None
