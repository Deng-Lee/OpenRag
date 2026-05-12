"""Tests for task management functionality"""

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models.base import Base
from openrag.models.task import Task, TaskStatus, TaskType
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.task_service import TaskService
from openrag.services.quota_service import QuotaService, QuotaExceededError
from openrag.services.priority_strategy import (
    FileSizeStrategy,
    PriorityStrategyRegistry,
    calculate_task_priority,
)


@pytest.fixture(scope="function")
def db_session():
    """Create test database session"""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def test_user(db_session):
    """Create test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash="hash",
        full_name="Test User",
        is_active=True
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_workspace(db_session, test_user):
    """Create test workspace"""
    workspace = Workspace(
        name="Test Workspace",
        slug="test-workspace",
        description="Test workspace",
        owner_id=test_user.id,
        max_concurrent_tasks=5
    )
    db_session.add(workspace)
    db_session.commit()
    db_session.refresh(workspace)
    return workspace


class TestTaskModel:
    """Test Task model"""

    def test_create_task(self, db_session, test_user, test_workspace):
        """Test creating a task"""
        task = Task(
            task_id="test-task-123",
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            task_type=TaskType.PROCESS_DOCUMENT,
            queue="normal",
            priority=5,
            status=TaskStatus.PENDING,
            max_retries=3
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        assert task.id is not None
        assert task.task_id == "test-task-123"
        assert task.workspace_id == test_workspace.id
        assert task.user_id == test_user.id
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 0
        assert task.progress == 0

    def test_task_to_dict(self, db_session, test_user, test_workspace):
        """Test task serialization"""
        task = Task(
            task_id="test-task-456",
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            task_type=TaskType.PROCESS_DOCUMENT,
            queue="fast",
            priority=8,
            status=TaskStatus.SUCCESS,
            progress=100,
            retry_count=0,
            max_retries=3,
            result={"chunks": 10}
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        data = task.to_dict()
        assert data["task_id"] == "test-task-456"
        assert data["queue"] == "fast"
        assert data["priority"] == 8
        assert data["status"] == "success"
        assert data["progress"] == 100
        assert data["result"] == {"chunks": 10}


class TestTaskService:
    """Test TaskService"""

    def test_create_task(self, db_session, test_user, test_workspace):
        """Test creating task via service"""
        service = TaskService(db_session)
        task = service.create_task(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            task_type="process_document",
            queue="normal",
            priority=5,
            max_retries=3
        )

        assert task.id is not None
        assert task.task_id is not None  # Auto-generated UUID
        assert task.status == TaskStatus.PENDING

    def test_get_task(self, db_session, test_user, test_workspace):
        """Test getting task by ID"""
        service = TaskService(db_session)
        task = service.create_task(
            workspace_id=test_workspace.id,
            user_id=test_user.id
        )

        found = service.get_task(task.id)
        assert found is not None
        assert found.id == task.id

    def test_list_tasks(self, db_session, test_user, test_workspace):
        """Test listing tasks with filtering"""
        service = TaskService(db_session)

        # Create multiple tasks
        for i in range(3):
            service.create_task(
                workspace_id=test_workspace.id,
                user_id=test_user.id,
                status=TaskStatus.PENDING if i < 2 else TaskStatus.SUCCESS
            )

        # List all tasks
        tasks, total = service.list_tasks(workspace_id=test_workspace.id)
        assert total == 3
        assert len(tasks) == 3

        # Filter by status
        tasks, total = service.list_tasks(
            workspace_id=test_workspace.id,
            status=TaskStatus.PENDING
        )
        assert total == 2

    def test_update_task_status(self, db_session, test_user, test_workspace):
        """Test updating task status"""
        service = TaskService(db_session)
        task = service.create_task(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            status=TaskStatus.PENDING
        )

        # Update to started
        updated = service.update_task_status(
            task.id,
            TaskStatus.STARTED,
            progress=50
        )
        assert updated.status == TaskStatus.STARTED
        assert updated.progress == 50
        assert updated.started_at is not None

        # Update to success
        updated = service.update_task_status(
            task.id,
            TaskStatus.SUCCESS,
            progress=100,
            result={"chunks": 10}
        )
        assert updated.status == TaskStatus.SUCCESS
        assert updated.completed_at is not None
        assert updated.result == {"chunks": 10}

    def test_cancel_task(self, db_session, test_user, test_workspace):
        """Test cancelling a task"""
        service = TaskService(db_session)
        task = service.create_task(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            status=TaskStatus.PENDING
        )

        cancelled = service.cancel_task(task.id)
        assert cancelled is not None
        assert cancelled.status == TaskStatus.CANCELLED
        assert cancelled.completed_at is not None

    def test_cancel_completed_task_fails(self, db_session, test_user, test_workspace):
        """Test cancelling already completed task fails"""
        service = TaskService(db_session)
        task = service.create_task(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            status=TaskStatus.SUCCESS
        )
        db_session.commit()

        cancelled = service.cancel_task(task.id)
        assert cancelled is None  # Cannot cancel completed task

    def test_retry_task(self, db_session, test_user, test_workspace):
        """Test retrying a failed task"""
        service = TaskService(db_session)
        task = service.create_task(
            workspace_id=test_workspace.id,
            user_id=test_user.id,
            status=TaskStatus.FAILURE,
            max_retries=3
        )
        task.retry_count = 1
        db_session.commit()

        retried = service.retry_task(task.id)
        assert retried is not None
        assert retried.status == TaskStatus.PENDING
        assert retried.retry_count == 2
        assert retried.completed_at is None

    def test_get_task_stats(self, db_session, test_user, test_workspace):
        """Test getting task statistics"""
        service = TaskService(db_session)

        # Create tasks with different statuses
        service.create_task(workspace_id=test_workspace.id, user_id=test_user.id, status=TaskStatus.PENDING)
        service.create_task(workspace_id=test_workspace.id, user_id=test_user.id, status=TaskStatus.STARTED)
        service.create_task(workspace_id=test_workspace.id, user_id=test_user.id, status=TaskStatus.SUCCESS)
        service.create_task(workspace_id=test_workspace.id, user_id=test_user.id, status=TaskStatus.FAILURE)

        stats = service.get_task_stats(workspace_id=test_workspace.id)
        assert stats["total"] == 4
        assert stats["running"] == 1
        assert stats["pending"] == 1
        assert stats["by_status"]["pending"] == 1
        assert stats["by_status"]["started"] == 1
        assert stats["by_status"]["success"] == 1
        assert stats["by_status"]["failure"] == 1


class TestQuotaService:
    """Test QuotaService"""

    def test_get_running_tasks_count(self, db_session, test_user, test_workspace):
        """Test getting running tasks count"""
        service = TaskService(db_session)
        quota_service = QuotaService(db_session)

        # Create running tasks
        for _ in range(3):
            service.create_task(
                workspace_id=test_workspace.id,
                user_id=test_user.id,
                status=TaskStatus.STARTED
            )

        count = quota_service.get_running_tasks_count(test_workspace.id, use_cache=False)
        assert count == 3

    def test_check_workspace_quota(self, db_session, test_user, test_workspace):
        """Test quota checking"""
        service = TaskService(db_session)
        quota_service = QuotaService(db_session)

        # Create tasks up to limit
        for _ in range(5):
            service.create_task(
                workspace_id=test_workspace.id,
                user_id=test_user.id,
                status=TaskStatus.STARTED
            )

        # Should raise QuotaExceededError
        with pytest.raises(QuotaExceededError):
            quota_service.check_workspace_quota(test_workspace.id)

    def test_get_quota_usage(self, db_session, test_user, test_workspace):
        """Test getting quota usage"""
        service = TaskService(db_session)
        quota_service = QuotaService(db_session)

        # Create running and pending tasks
        service.create_task(workspace_id=test_workspace.id, user_id=test_user.id, status=TaskStatus.STARTED)
        service.create_task(workspace_id=test_workspace.id, user_id=test_user.id, status=TaskStatus.PENDING)

        usage = quota_service.get_quota_usage(test_workspace.id)
        assert usage["max_concurrent_tasks"] == 5
        assert usage["running_tasks"] == 2  # STARTED + PENDING
        assert usage["pending_tasks"] == 1
        assert usage["available_slots"] == 3


class TestPriorityStrategy:
    """Test PriorityStrategy"""

    def test_file_size_strategy_small_file(self):
        """Test FileSizeStrategy for small files"""
        strategy = FileSizeStrategy()
        queue, priority = strategy.calculate_priority(
            file_size=500_000,  # 500KB
            file_type="text/plain",
            user=None,
            workspace=None
        )
        assert queue == "fast"
        assert priority == 8

    def test_file_size_strategy_medium_file(self):
        """Test FileSizeStrategy for medium files"""
        strategy = FileSizeStrategy()
        queue, priority = strategy.calculate_priority(
            file_size=5_000_000,  # 5MB
            file_type="application/pdf",
            user=None,
            workspace=None
        )
        assert queue == "normal"
        assert priority == 5

    def test_file_size_strategy_large_file(self):
        """Test FileSizeStrategy for large files"""
        strategy = FileSizeStrategy()
        queue, priority = strategy.calculate_priority(
            file_size=20_000_000,  # 20MB
            file_type="video/mp4",
            user=None,
            workspace=None
        )
        assert queue == "slow"
        assert priority == 2

    def test_strategy_registry(self):
        """Test PriorityStrategyRegistry"""
        strategies = PriorityStrategyRegistry.list_strategies()
        assert "file_size" in strategies
        assert "user_role" in strategies
        assert "file_type" in strategies
        assert "hybrid" in strategies

        # Get strategy
        strategy = PriorityStrategyRegistry.get_strategy("file_size")
        assert isinstance(strategy, FileSizeStrategy)

        # Check exists
        assert PriorityStrategyRegistry.has_strategy("file_size")
        assert not PriorityStrategyRegistry.has_strategy("unknown")

    def test_calculate_task_priority(self):
        """Test calculate_task_priority helper"""
        queue, priority = calculate_task_priority(
            file_size=500_000,
            file_type="text/plain",
            user=None,
            workspace=None,
            strategy_name="file_size"
        )
        assert queue == "fast"
        assert priority == 8
