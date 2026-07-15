"""Delayed retry scheduling and broker readiness tests."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.broker.task_broker import TaskBroker
from openrag.models.base import Base
from openrag.models.task import TaskStatus
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.task_service import TaskService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user = User(
        username="retry-user",
        email="retry@example.com",
        password_hash="hash",
        full_name="Retry User",
        is_active=True,
    )
    session.add(user)
    session.flush()
    workspace = Workspace(
        name="Retry Workspace",
        slug="retry-workspace",
        owner_id=user.id,
        max_concurrent_tasks=5,
    )
    session.add(workspace)
    session.commit()
    yield session, user, workspace
    session.close()
    Base.metadata.drop_all(engine)


def new_task(db, user, workspace):
    return TaskService(db).create_task(
        workspace_id=workspace.id,
        user_id=user.id,
        max_retries=3,
    )


def test_schedule_retry_sets_delay_and_clears_assignment(db):
    session, user, workspace = db
    task = new_task(session, user, workspace)
    task.worker_id = "worker-old"
    task.assigned_at = datetime.now(timezone.utc)
    task.heartbeat_at = datetime.now(timezone.utc)
    session.commit()

    retried = TaskService(session).schedule_task_retry(
        task.id,
        error="EMBEDDING_TIMEOUT: timed out",
        error_code="EMBEDDING_TIMEOUT",
        delay_seconds=30,
    )

    assert retried.status == TaskStatus.RETRY
    assert retried.retry_count == 1
    assert retried.error_retryable is True
    assert retried.next_retry_at is not None
    assert retried.worker_id is None
    assert retried.assigned_at is None
    assert retried.heartbeat_at is None


def test_retry_not_due_is_not_assigned_but_due_retry_is(db):
    session, user, workspace = db
    task = new_task(session, user, workspace)
    task.status = TaskStatus.RETRY.value
    task.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    session.commit()

    assert TaskBroker(session).get_tasks("worker-one", 1) == []

    task.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()
    assigned = TaskBroker(session).get_tasks("worker-one", 1)

    assert [row.id for row in assigned] == [task.id]
    assert assigned[0].status == TaskStatus.ASSIGNED


def test_two_assignments_cannot_claim_same_due_retry(db):
    session, user, workspace = db
    task = new_task(session, user, workspace)
    task.status = TaskStatus.RETRY.value
    task.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()

    first = TaskBroker(session).get_tasks("worker-one", 1)
    second = TaskBroker(session).get_tasks("worker-two", 1)

    assert [row.id for row in first] == [task.id]
    assert second == []


def test_success_clears_old_retry_errors(db):
    session, user, workspace = db
    task = new_task(session, user, workspace)
    task.error = "old"
    task.error_code = "EMBEDDING_TIMEOUT"
    task.error_retryable = True
    task.next_retry_at = datetime.now(timezone.utc)
    session.commit()

    updated = TaskService(session).update_task_status(task.id, TaskStatus.SUCCESS)

    assert updated.error is None
    assert updated.error_code is None
    assert updated.error_retryable is False
    assert updated.next_retry_at is None
