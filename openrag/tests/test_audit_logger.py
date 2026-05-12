"""Tests for audit logging service"""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models.base import Base
from openrag.models.user import User
from openrag.models.audit import AuditLog
from openrag.services.audit_logger import AuditLogger


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
def audit_logger(session):
    """Create AuditLogger instance"""
    return AuditLogger(session)


@pytest.fixture
def test_user(session):
    """Create a test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash="hashed_password",
        full_name="Test User",
        is_active=True
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


class TestLogOperation:
    """Test logging operations"""

    def test_log_operation(self, audit_logger, test_user):
        """Test logging a basic operation"""
        log = audit_logger.log(
            user_id=test_user.id,
            action="create",
            resource_type="file",
            resource_id=1
        )

        assert log.id is not None
        assert log.user_id == test_user.id
        assert log.action == "create"
        assert log.resource_type == "file"
        assert log.resource_id == 1
        assert log.details is None
        assert log.ip_address is None
        assert log.created_at is not None

    def test_log_with_details(self, audit_logger, test_user):
        """Test logging with details field"""
        details = '{"filename": "test.txt", "size": 1024}'
        log = audit_logger.log(
            user_id=test_user.id,
            action="update",
            resource_type="file",
            resource_id=1,
            details=details
        )

        assert log.details == details

    def test_log_with_ip_address(self, audit_logger, test_user):
        """Test logging with IP address"""
        log = audit_logger.log(
            user_id=test_user.id,
            action="read",
            resource_type="file",
            resource_id=1,
            ip_address="192.168.1.1"
        )

        assert log.ip_address == "192.168.1.1"

    def test_log_invalid_user(self, audit_logger):
        """Test logging with non-existent user raises error"""
        with pytest.raises(ValueError, match="User with id 999 does not exist"):
            audit_logger.log(
                user_id=999,
                action="create",
                resource_type="file",
                resource_id=1
            )


class TestGetUserLogs:
    """Test getting user logs"""

    def test_get_user_logs(self, audit_logger, test_user):
        """Test getting logs for a specific user"""
        # Create multiple logs
        audit_logger.log(test_user.id, "create", "file", 1)
        audit_logger.log(test_user.id, "update", "file", 1)
        audit_logger.log(test_user.id, "delete", "file", 1)

        logs = audit_logger.get_user_logs(test_user.id)

        assert len(logs) == 3
        # Verify all logs belong to the user
        for log in logs:
            assert log.user_id == test_user.id

    def test_get_user_logs_pagination(self, audit_logger, test_user):
        """Test pagination works"""
        # Create 5 logs
        for i in range(5):
            audit_logger.log(test_user.id, "read", "file", i)

        # Get first 2
        logs = audit_logger.get_user_logs(test_user.id, limit=2, offset=0)
        assert len(logs) == 2

        # Get next 2
        logs = audit_logger.get_user_logs(test_user.id, limit=2, offset=2)
        assert len(logs) == 2

        # Get last 1
        logs = audit_logger.get_user_logs(test_user.id, limit=2, offset=4)
        assert len(logs) == 1

    def test_get_user_logs_empty(self, audit_logger, session):
        """Test returning empty list for user with no logs"""
        # Create user without logs
        user = User(
            username="nouser",
            email="no@example.com",
            password_hash="hashed",
            full_name="No Logs",
            is_active=True
        )
        session.add(user)
        session.commit()
        session.refresh(user)

        logs = audit_logger.get_user_logs(user.id)
        assert logs == []


class TestGetResourceLogs:
    """Test getting resource logs"""

    def test_get_resource_logs(self, audit_logger, test_user, session):
        """Test getting logs for a specific resource"""
        # Create another user
        user2 = User(
            username="user2",
            email="user2@example.com",
            password_hash="hashed",
            full_name="User 2",
            is_active=True
        )
        session.add(user2)
        session.commit()
        session.refresh(user2)

        # Create logs for same resource from different users
        audit_logger.log(test_user.id, "create", "file", 1)
        audit_logger.log(user2.id, "read", "file", 1)
        audit_logger.log(test_user.id, "update", "file", 1)

        logs = audit_logger.get_resource_logs("file", 1)

        assert len(logs) == 3
        # Verify all logs are for the same resource
        for log in logs:
            assert log.resource_type == "file"
            assert log.resource_id == 1

    def test_get_resource_logs_pagination(self, audit_logger, test_user):
        """Test pagination works"""
        # Create 5 logs for same resource
        for i in range(5):
            audit_logger.log(test_user.id, "read", "file", 1)

        # Get first 2
        logs = audit_logger.get_resource_logs("file", 1, limit=2, offset=0)
        assert len(logs) == 2

        # Get next 2
        logs = audit_logger.get_resource_logs("file", 1, limit=2, offset=2)
        assert len(logs) == 2


class TestGetLogsByAction:
    """Test getting logs by action type"""

    def test_get_logs_by_action(self, audit_logger, test_user):
        """Test getting logs by action type"""
        # Create logs with different actions
        audit_logger.log(test_user.id, "create", "file", 1)
        audit_logger.log(test_user.id, "create", "file", 2)
        audit_logger.log(test_user.id, "update", "file", 1)
        audit_logger.log(test_user.id, "create", "team", 1)

        logs = audit_logger.get_logs_by_action("create")

        assert len(logs) == 3
        for log in logs:
            assert log.action == "create"

    def test_get_logs_by_action_pagination(self, audit_logger, test_user):
        """Test pagination works"""
        # Create 5 logs with same action
        for i in range(5):
            audit_logger.log(test_user.id, "delete", "file", i)

        # Get first 2
        logs = audit_logger.get_logs_by_action("delete", limit=2, offset=0)
        assert len(logs) == 2

        # Get next 2
        logs = audit_logger.get_logs_by_action("delete", limit=2, offset=2)
        assert len(logs) == 2


class TestGetLogsInRange:
    """Test getting logs within time range"""

    def test_get_logs_in_range(self, audit_logger, test_user, session):
        """Test getting logs within time range"""
        # Create logs
        log1 = audit_logger.log(test_user.id, "create", "file", 1)
        log2 = audit_logger.log(test_user.id, "update", "file", 1)
        log3 = audit_logger.log(test_user.id, "delete", "file", 1)

        # Query with time range
        start_time = log1.created_at - timedelta(seconds=1)
        end_time = log3.created_at + timedelta(seconds=1)

        logs = audit_logger.get_logs_in_range(start_time, end_time)

        assert len(logs) == 3

    def test_get_logs_in_range_with_user_filter(self, audit_logger, test_user, session):
        """Test filtering by user_id"""
        # Create another user
        user2 = User(
            username="user2",
            email="user2@example.com",
            password_hash="hashed",
            full_name="User 2",
            is_active=True
        )
        session.add(user2)
        session.commit()
        session.refresh(user2)

        # Create logs from both users
        log1 = audit_logger.log(test_user.id, "create", "file", 1)
        audit_logger.log(user2.id, "create", "file", 2)
        log3 = audit_logger.log(test_user.id, "update", "file", 1)

        # Query with user filter
        start_time = log1.created_at - timedelta(seconds=1)
        end_time = log3.created_at + timedelta(seconds=1)

        logs = audit_logger.get_logs_in_range(start_time, end_time, user_id=test_user.id)

        assert len(logs) == 2
        for log in logs:
            assert log.user_id == test_user.id

    def test_get_logs_in_range_with_action_filter(self, audit_logger, test_user):
        """Test filtering by action"""
        # Create logs with different actions
        log1 = audit_logger.log(test_user.id, "create", "file", 1)
        audit_logger.log(test_user.id, "update", "file", 1)
        log3 = audit_logger.log(test_user.id, "create", "file", 2)

        # Query with action filter
        start_time = log1.created_at - timedelta(seconds=1)
        end_time = log3.created_at + timedelta(seconds=1)

        logs = audit_logger.get_logs_in_range(start_time, end_time, action="create")

        assert len(logs) == 2
        for log in logs:
            assert log.action == "create"

    def test_get_logs_in_range_with_both_filters(self, audit_logger, test_user, session):
        """Test filtering by both user_id and action"""
        # Create another user
        user2 = User(
            username="user2",
            email="user2@example.com",
            password_hash="hashed",
            full_name="User 2",
            is_active=True
        )
        session.add(user2)
        session.commit()
        session.refresh(user2)

        # Create various logs
        log1 = audit_logger.log(test_user.id, "create", "file", 1)
        audit_logger.log(test_user.id, "update", "file", 1)
        audit_logger.log(user2.id, "create", "file", 2)
        log4 = audit_logger.log(test_user.id, "create", "file", 3)

        # Query with both filters
        start_time = log1.created_at - timedelta(seconds=1)
        end_time = log4.created_at + timedelta(seconds=1)

        logs = audit_logger.get_logs_in_range(
            start_time, end_time,
            user_id=test_user.id,
            action="create"
        )

        assert len(logs) == 2
        for log in logs:
            assert log.user_id == test_user.id
            assert log.action == "create"


class TestLogsOrdering:
    """Test logs are properly ordered"""

    def test_logs_ordered_by_time(self, audit_logger, test_user, session):
        """Test logs are ordered by created_at DESC"""
        # Create logs and manually set different timestamps
        from datetime import timedelta

        log1 = audit_logger.log(test_user.id, "create", "file", 1)

        # Manually update timestamp for second log
        log2 = audit_logger.log(test_user.id, "update", "file", 1)
        log2.created_at = log1.created_at + timedelta(seconds=1)
        session.commit()

        # Manually update timestamp for third log
        log3 = audit_logger.log(test_user.id, "delete", "file", 1)
        log3.created_at = log2.created_at + timedelta(seconds=1)
        session.commit()

        logs = audit_logger.get_user_logs(test_user.id)

        # Verify descending order
        assert logs[0].created_at >= logs[1].created_at
        assert logs[1].created_at >= logs[2].created_at

        # Verify actions are in reverse order
        assert logs[0].action == "delete"
        assert logs[1].action == "update"
        assert logs[2].action == "create"
