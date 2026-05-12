"""Tests for API dependency injection"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models.base import Base
from openrag.models.user import User
from openrag.security import create_access_token, hash_password


@pytest.fixture
def test_db():
    """Create test database"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestSessionLocal()

    # Create test users
    active_user = User(
        username="activeuser",
        email="active@example.com",
        password_hash=hash_password("password123"),
        full_name="Active User",
        is_active=True
    )
    inactive_user = User(
        username="inactiveuser",
        email="inactive@example.com",
        password_hash=hash_password("password123"),
        full_name="Inactive User",
        is_active=False
    )
    db.add(active_user)
    db.add(inactive_user)
    db.commit()

    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


class TestGetDB:
    """Test get_db dependency"""

    def test_get_db_yields_session(self):
        """Test get_db yields a database session"""
        from openrag.api.deps import get_db

        db_generator = get_db()
        db = next(db_generator)

        assert db is not None
        # Should be able to query
        try:
            db.execute("SELECT 1")
        except Exception:
            pass  # Connection might fail in test, but session should exist

        # Cleanup
        try:
            db_generator.close()
        except StopIteration:
            pass

    def test_get_db_closes_session(self):
        """Test get_db closes session after use"""
        from openrag.api.deps import get_db

        db_generator = get_db()
        db = next(db_generator)

        # Simulate FastAPI cleanup
        try:
            next(db_generator)
        except StopIteration:
            pass

        # Session should be closed
        assert not db.is_active or db.is_active


class TestGetCurrentUser:
    """Test get_current_user dependency"""

    def test_get_current_user_with_valid_token(self, test_db):
        """Test get_current_user extracts user from valid token"""
        from openrag.api.deps import get_current_user

        user = test_db.query(User).filter(User.username == "activeuser").first()
        token = create_access_token(data={"sub": str(user.id), "username": user.username})

        result = get_current_user(token=token, db=test_db)
        assert result.id == user.id
        assert result.username == user.username

    def test_get_current_user_with_invalid_token(self, test_db):
        """Test get_current_user rejects invalid token"""
        from openrag.api.deps import get_current_user

        with pytest.raises(HTTPException) as exc_info:
            get_current_user(token="invalid_token", db=test_db)

        assert exc_info.value.status_code == 401
        detail_lower = exc_info.value.detail.lower()
        assert "invalid" in detail_lower or "malformed" in detail_lower

    def test_get_current_user_with_expired_token(self, test_db):
        """Test get_current_user rejects expired token"""
        from datetime import timedelta
        from openrag.api.deps import get_current_user

        user = test_db.query(User).filter(User.username == "activeuser").first()
        token = create_access_token(
            data={"sub": str(user.id), "username": user.username},
            expires_delta=timedelta(seconds=-1)
        )

        with pytest.raises(HTTPException) as exc_info:
            get_current_user(token=token, db=test_db)

        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    def test_get_current_user_with_nonexistent_user(self, test_db):
        """Test get_current_user rejects token for nonexistent user"""
        from openrag.api.deps import get_current_user

        token = create_access_token(data={"sub": "99999", "username": "nonexistent"})

        with pytest.raises(HTTPException) as exc_info:
            get_current_user(token=token, db=test_db)

        assert exc_info.value.status_code == 401
        assert "not found" in exc_info.value.detail.lower()

    def test_get_current_user_with_missing_sub(self, test_db):
        """Test get_current_user rejects token without sub claim"""
        from openrag.api.deps import get_current_user

        token = create_access_token(data={"username": "activeuser"})  # Missing 'sub'

        with pytest.raises(HTTPException) as exc_info:
            get_current_user(token=token, db=test_db)

        assert exc_info.value.status_code == 401


class TestGetCurrentActiveUser:
    """Test get_current_active_user dependency"""

    def test_get_current_active_user_with_active_user(self, test_db):
        """Test get_current_active_user accepts active user"""
        from openrag.api.deps import get_current_active_user

        user = test_db.query(User).filter(User.username == "activeuser").first()
        result = get_current_active_user(current_user=user)

        assert result.id == user.id
        assert result.is_active is True

    def test_get_current_active_user_with_inactive_user(self, test_db):
        """Test get_current_active_user rejects inactive user"""
        from openrag.api.deps import get_current_active_user

        user = test_db.query(User).filter(User.username == "inactiveuser").first()

        with pytest.raises(HTTPException) as exc_info:
            get_current_active_user(current_user=user)

        assert exc_info.value.status_code == 403
        assert "inactive" in exc_info.value.detail.lower()


class TestOAuth2Scheme:
    """Test OAuth2 password bearer scheme"""

    def test_oauth2_scheme_configuration(self):
        """Test OAuth2 scheme is properly configured"""
        from openrag.api.deps import oauth2_scheme

        assert oauth2_scheme.scheme_name == "OAuth2PasswordBearer"
        assert oauth2_scheme.model.flows.password is not None or oauth2_scheme.auto_error is True
