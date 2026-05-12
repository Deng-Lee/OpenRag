"""Tests for User Management API"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.main import app
from openrag.api.deps import get_db
from openrag.models.base import Base
from openrag.models.user import User
from openrag.security import hash_password, create_access_token


# Test database setup
@pytest.fixture(scope="function")
def engine():
    """Create test database engine"""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture(scope="function")
def db(engine):
    """Create test database session"""
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        """Override database dependency for testing"""
        try:
            db = TestingSessionLocal()
            yield db
        finally:
            db.close()

    # Override dependencies
    app.dependency_overrides[get_db] = override_get_db

    db = TestingSessionLocal()
    yield db
    db.close()

    # Clean up override
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


@pytest.fixture
def test_user(db):
    """Create test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=hash_password("testpass123"),
        full_name="Test User",
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def auth_token(test_user):
    """Create authentication token for test user"""
    return create_access_token(data={"sub": str(test_user.id)})


@pytest.fixture
def auth_headers(auth_token):
    """Create authorization headers"""
    return {"Authorization": f"Bearer {auth_token}"}


class TestUserRegistration:
    """Test user registration endpoint"""

    def test_register_user_success(self, client, db):
        """Test successful user registration"""
        response = client.post(
            "/users/register",
            json={
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "password123",
                "full_name": "New User"
            }
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["username"] == "newuser"
        assert data["email"] == "newuser@example.com"
        assert data["full_name"] == "New User"
        assert data["is_active"] is True
        assert "id" in data
        assert "password" not in data
        assert "password_hash" not in data

    def test_register_user_duplicate_username(self, client, test_user):
        """Test registration with duplicate username"""
        response = client.post(
            "/users/register",
            json={
                "username": "testuser",
                "email": "different@example.com",
                "password": "password123",
                "full_name": "Different User"
            }
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in response.json()["detail"].lower()

    def test_register_user_duplicate_email(self, client, test_user):
        """Test registration with duplicate email"""
        response = client.post(
            "/users/register",
            json={
                "username": "differentuser",
                "email": "test@example.com",
                "password": "password123",
                "full_name": "Different User"
            }
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in response.json()["detail"].lower()

    def test_register_user_missing_fields(self, client):
        """Test registration with missing required fields"""
        response = client.post(
            "/users/register",
            json={
                "username": "newuser",
                "email": "newuser@example.com"
            }
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestUserLogin:
    """Test user login endpoint"""

    def test_login_success(self, client, test_user):
        """Test successful login"""
        response = client.post(
            "/users/login",
            json={
                "email": "test@example.com",
                "password": "testpass123"
            }
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "user" in data
        assert data["user"]["id"] == test_user.id
        assert data["user"]["username"] == test_user.username

    def test_login_wrong_password(self, client, test_user):
        """Test login with wrong password"""
        response = client.post(
            "/users/login",
            json={
                "email": "test@example.com",
                "password": "wrongpassword"
            }
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "incorrect" in response.json()["detail"].lower()

    def test_login_nonexistent_user(self, client, db):
        """Test login with non-existent user"""
        response = client.post(
            "/users/login",
            json={
                "email": "nonexistent@example.com",
                "password": "password123"
            }
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_login_inactive_user(self, client, db, test_user):
        """Test login with inactive user"""
        test_user.is_active = False
        db.commit()

        response = client.post(
            "/users/login",
            json={
                "email": "test@example.com",
                "password": "testpass123"
            }
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestGetCurrentUser:
    """Test get current user profile endpoint"""

    def test_get_current_user_success(self, client, test_user, auth_headers):
        """Test getting current user profile"""
        response = client.get("/users/me", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == test_user.id
        assert data["username"] == test_user.username
        assert data["email"] == test_user.email
        assert data["full_name"] == test_user.full_name
        assert "password_hash" not in data

    def test_get_current_user_no_token(self, client):
        """Test getting current user without token"""
        response = client.get("/users/me")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_get_current_user_invalid_token(self, client):
        """Test getting current user with invalid token"""
        response = client.get(
            "/users/me",
            headers={"Authorization": "Bearer invalid_token"}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestUpdateCurrentUser:
    """Test update current user profile endpoint"""

    def test_update_current_user_success(self, client, test_user, auth_headers):
        """Test updating current user profile"""
        response = client.put(
            "/users/me",
            headers=auth_headers,
            json={
                "full_name": "Updated Name",
                "email": "updated@example.com"
            }
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["full_name"] == "Updated Name"
        assert data["email"] == "updated@example.com"
        assert data["username"] == test_user.username

    def test_update_current_user_duplicate_email(self, client, db, test_user, auth_headers):
        """Test updating with duplicate email"""
        # Create another user
        other_user = User(
            username="otheruser",
            email="other@example.com",
            password_hash=hash_password("password123"),
            full_name="Other User",
            is_active=True
        )
        db.add(other_user)
        db.commit()

        response = client.put(
            "/users/me",
            headers=auth_headers,
            json={"email": "other@example.com"}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_update_current_user_no_token(self, client):
        """Test updating without token"""
        response = client.put(
            "/users/me",
            json={"full_name": "Updated Name"}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestGetUserById:
    """Test get user by ID endpoint"""

    def test_get_user_by_id_success(self, client, test_user, auth_headers):
        """Test getting user by ID"""
        response = client.get(f"/users/{test_user.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == test_user.id
        assert data["username"] == test_user.username
        assert "password_hash" not in data

    def test_get_user_by_id_not_found(self, client, auth_headers):
        """Test getting non-existent user"""
        response = client.get("/users/99999", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_user_by_id_no_token(self, client, test_user):
        """Test getting user without token"""
        response = client.get(f"/users/{test_user.id}")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
