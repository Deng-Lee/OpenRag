"""Tests for user management service"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models.base import Base
from openrag.models.user import User
from openrag.security import hash_password, verify_password
from openrag.services.user_manager import UserManager


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
def user_manager(session):
    """Create UserManager instance"""
    return UserManager(session)


class TestUserCreation:
    """Test user creation"""

    def test_create_user_success(self, user_manager):
        """Test successful user creation"""
        user = user_manager.create_user(
            username="testuser",
            email="test@example.com",
            password="password123",
            full_name="Test User"
        )

        assert user.id is not None
        assert user.username == "testuser"
        assert user.email == "test@example.com"
        assert user.full_name == "Test User"
        assert user.is_active is True
        assert verify_password("password123", user.password_hash)

    def test_create_user_duplicate_username(self, user_manager):
        """Test creating user with duplicate username"""
        user_manager.create_user(
            username="testuser",
            email="test1@example.com",
            password="password123",
            full_name="Test User 1"
        )

        with pytest.raises(ValueError, match="Username 'testuser' already exists"):
            user_manager.create_user(
                username="testuser",
                email="test2@example.com",
                password="password123",
                full_name="Test User 2"
            )

    def test_create_user_duplicate_email(self, user_manager):
        """Test creating user with duplicate email"""
        user_manager.create_user(
            username="testuser1",
            email="test@example.com",
            password="password123",
            full_name="Test User 1"
        )

        with pytest.raises(ValueError, match="Email 'test@example.com' already exists"):
            user_manager.create_user(
                username="testuser2",
                email="test@example.com",
                password="password123",
                full_name="Test User 2"
            )

    def test_create_user_empty_username(self, user_manager):
        """Test creating user with empty username"""
        with pytest.raises(ValueError, match="Username cannot be empty"):
            user_manager.create_user(
                username="",
                email="test@example.com",
                password="password123",
                full_name="Test User"
            )

    def test_create_user_empty_email(self, user_manager):
        """Test creating user with empty email"""
        with pytest.raises(ValueError, match="Email cannot be empty"):
            user_manager.create_user(
                username="testuser",
                email="",
                password="password123",
                full_name="Test User"
            )

    def test_create_user_empty_password(self, user_manager):
        """Test creating user with empty password"""
        with pytest.raises(ValueError, match="Password cannot be empty"):
            user_manager.create_user(
                username="testuser",
                email="test@example.com",
                password="",
                full_name="Test User"
            )

    def test_create_user_empty_full_name(self, user_manager):
        """Test creating user with empty full name"""
        with pytest.raises(ValueError, match="Full name cannot be empty"):
            user_manager.create_user(
                username="testuser",
                email="test@example.com",
                password="password123",
                full_name=""
            )


class TestUserRetrieval:
    """Test user retrieval methods"""

    @pytest.fixture
    def sample_user(self, user_manager):
        """Create a sample user for testing"""
        return user_manager.create_user(
            username="testuser",
            email="test@example.com",
            password="password123",
            full_name="Test User"
        )

    def test_get_user_by_id_success(self, user_manager, sample_user):
        """Test getting user by ID"""
        user = user_manager.get_user_by_id(sample_user.id)
        assert user is not None
        assert user.id == sample_user.id
        assert user.username == "testuser"

    def test_get_user_by_id_not_found(self, user_manager):
        """Test getting non-existent user by ID"""
        user = user_manager.get_user_by_id(99999)
        assert user is None

    def test_get_user_by_username_success(self, user_manager, sample_user):
        """Test getting user by username"""
        user = user_manager.get_user_by_username("testuser")
        assert user is not None
        assert user.id == sample_user.id
        assert user.username == "testuser"

    def test_get_user_by_username_not_found(self, user_manager):
        """Test getting non-existent user by username"""
        user = user_manager.get_user_by_username("nonexistent")
        assert user is None

    def test_get_user_by_email_success(self, user_manager, sample_user):
        """Test getting user by email"""
        user = user_manager.get_user_by_email("test@example.com")
        assert user is not None
        assert user.id == sample_user.id
        assert user.email == "test@example.com"

    def test_get_user_by_email_not_found(self, user_manager):
        """Test getting non-existent user by email"""
        user = user_manager.get_user_by_email("nonexistent@example.com")
        assert user is None


class TestUserUpdate:
    """Test user update operations"""

    @pytest.fixture
    def sample_user(self, user_manager):
        """Create a sample user for testing"""
        return user_manager.create_user(
            username="testuser",
            email="test@example.com",
            password="password123",
            full_name="Test User"
        )

    def test_update_user_username(self, user_manager, sample_user):
        """Test updating username"""
        updated = user_manager.update_user(sample_user.id, username="newusername")
        assert updated is not None
        assert updated.username == "newusername"
        assert updated.email == "test@example.com"

    def test_update_user_email(self, user_manager, sample_user):
        """Test updating email"""
        updated = user_manager.update_user(sample_user.id, email="newemail@example.com")
        assert updated is not None
        assert updated.email == "newemail@example.com"
        assert updated.username == "testuser"

    def test_update_user_password(self, user_manager, sample_user):
        """Test updating password"""
        updated = user_manager.update_user(sample_user.id, password="newpassword456")
        assert updated is not None
        assert verify_password("newpassword456", updated.password_hash)
        assert not verify_password("password123", updated.password_hash)

    def test_update_user_full_name(self, user_manager, sample_user):
        """Test updating full name"""
        updated = user_manager.update_user(sample_user.id, full_name="New Name")
        assert updated is not None
        assert updated.full_name == "New Name"

    def test_update_user_is_active(self, user_manager, sample_user):
        """Test updating is_active status"""
        updated = user_manager.update_user(sample_user.id, is_active=False)
        assert updated is not None
        assert updated.is_active is False

    def test_update_user_multiple_fields(self, user_manager, sample_user):
        """Test updating multiple fields at once"""
        updated = user_manager.update_user(
            sample_user.id,
            username="newusername",
            email="newemail@example.com",
            full_name="New Full Name"
        )
        assert updated is not None
        assert updated.username == "newusername"
        assert updated.email == "newemail@example.com"
        assert updated.full_name == "New Full Name"

    def test_update_user_not_found(self, user_manager):
        """Test updating non-existent user"""
        result = user_manager.update_user(99999, username="newusername")
        assert result is None

    def test_update_user_duplicate_username(self, user_manager, sample_user):
        """Test updating to duplicate username"""
        user_manager.create_user(
            username="otheruser",
            email="other@example.com",
            password="password123",
            full_name="Other User"
        )

        with pytest.raises(ValueError, match="Username 'otheruser' already exists"):
            user_manager.update_user(sample_user.id, username="otheruser")

    def test_update_user_duplicate_email(self, user_manager, sample_user):
        """Test updating to duplicate email"""
        user_manager.create_user(
            username="otheruser",
            email="other@example.com",
            password="password123",
            full_name="Other User"
        )

        with pytest.raises(ValueError, match="Email 'other@example.com' already exists"):
            user_manager.update_user(sample_user.id, email="other@example.com")

    def test_update_user_empty_username(self, user_manager, sample_user):
        """Test updating to empty username"""
        with pytest.raises(ValueError, match="Username cannot be empty"):
            user_manager.update_user(sample_user.id, username="")

    def test_update_user_empty_email(self, user_manager, sample_user):
        """Test updating to empty email"""
        with pytest.raises(ValueError, match="Email cannot be empty"):
            user_manager.update_user(sample_user.id, email="")

    def test_update_user_empty_password(self, user_manager, sample_user):
        """Test updating to empty password"""
        with pytest.raises(ValueError, match="Password cannot be empty"):
            user_manager.update_user(sample_user.id, password="")

    def test_update_user_empty_full_name(self, user_manager, sample_user):
        """Test updating to empty full name"""
        with pytest.raises(ValueError, match="Full name cannot be empty"):
            user_manager.update_user(sample_user.id, full_name="")


class TestUserDeletion:
    """Test user deletion"""

    @pytest.fixture
    def sample_user(self, user_manager):
        """Create a sample user for testing"""
        return user_manager.create_user(
            username="testuser",
            email="test@example.com",
            password="password123",
            full_name="Test User"
        )

    def test_delete_user_success(self, user_manager, sample_user):
        """Test successful user deletion"""
        result = user_manager.delete_user(sample_user.id)
        assert result is True

        # Verify user is deleted
        user = user_manager.get_user_by_id(sample_user.id)
        assert user is None

    def test_delete_user_not_found(self, user_manager):
        """Test deleting non-existent user"""
        result = user_manager.delete_user(99999)
        assert result is False


class TestUserAuthentication:
    """Test user authentication"""

    @pytest.fixture
    def sample_user(self, user_manager):
        """Create a sample user for testing"""
        return user_manager.create_user(
            username="testuser",
            email="test@example.com",
            password="password123",
            full_name="Test User"
        )

    def test_authenticate_user_success(self, user_manager, sample_user):
        """Test successful authentication"""
        user = user_manager.authenticate_user("testuser", "password123")
        assert user is not None
        assert user.id == sample_user.id
        assert user.username == "testuser"

    def test_authenticate_user_wrong_password(self, user_manager, sample_user):
        """Test authentication with wrong password"""
        user = user_manager.authenticate_user("testuser", "wrongpassword")
        assert user is None

    def test_authenticate_user_nonexistent_username(self, user_manager):
        """Test authentication with non-existent username"""
        user = user_manager.authenticate_user("nonexistent", "password123")
        assert user is None

    def test_authenticate_user_inactive(self, user_manager, sample_user):
        """Test authentication with inactive user"""
        user_manager.update_user(sample_user.id, is_active=False)
        user = user_manager.authenticate_user("testuser", "password123")
        assert user is None

    def test_authenticate_user_empty_username(self, user_manager):
        """Test authentication with empty username"""
        user = user_manager.authenticate_user("", "password123")
        assert user is None

    def test_authenticate_user_empty_password(self, user_manager, sample_user):
        """Test authentication with empty password"""
        user = user_manager.authenticate_user("testuser", "")
        assert user is None


class TestUserListing:
    """Test user listing with pagination"""

    @pytest.fixture
    def multiple_users(self, user_manager):
        """Create multiple users for testing"""
        users = []
        for i in range(15):
            user = user_manager.create_user(
                username=f"user{i}",
                email=f"user{i}@example.com",
                password="password123",
                full_name=f"User {i}"
            )
            users.append(user)
        return users

    def test_list_users_default(self, user_manager, multiple_users):
        """Test listing users with default pagination"""
        users = user_manager.list_users()
        assert len(users) == 15

    def test_list_users_with_limit(self, user_manager, multiple_users):
        """Test listing users with limit"""
        users = user_manager.list_users(limit=5)
        assert len(users) == 5

    def test_list_users_with_skip(self, user_manager, multiple_users):
        """Test listing users with skip"""
        users = user_manager.list_users(skip=10)
        assert len(users) == 5

    def test_list_users_with_skip_and_limit(self, user_manager, multiple_users):
        """Test listing users with skip and limit"""
        users = user_manager.list_users(skip=5, limit=5)
        assert len(users) == 5

    def test_list_users_empty(self, user_manager):
        """Test listing users when no users exist"""
        users = user_manager.list_users()
        assert len(users) == 0
