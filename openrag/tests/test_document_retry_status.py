import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models.base import Base
from openrag.models.file import File, ProcessingStatus
from openrag.models.task import Task, TaskStatus, TaskType
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.document_retry_status import (
    document_conflict_detail,
    document_processing_fields,
    task_retry_fields,
)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def test_user(db_session: Session) -> User:
    user = User(
        username="retry-user",
        email="retry@example.com",
        password_hash="hash",
        full_name="Retry User",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_workspace(db_session: Session, test_user: User) -> Workspace:
    workspace = Workspace(
        name="Retry Workspace",
        slug="retry-workspace",
        owner_id=test_user.id,
    )
    db_session.add(workspace)
    db_session.commit()
    db_session.refresh(workspace)
    return workspace


def _file(
    db_session: Session,
    test_user: User,
    test_workspace: Workspace,
    processing_status: ProcessingStatus = ProcessingStatus.pending,
) -> File:
    file = File(
        uri="/docs/report.pdf",
        name="report.pdf",
        owner_id=test_user.id,
        workspace_id=test_workspace.id,
        size=123,
        processing_status=processing_status,
    )
    db_session.add(file)
    db_session.commit()
    db_session.refresh(file)
    return file


def _task(
    db_session: Session,
    test_user: User,
    test_workspace: Workspace,
    file: File,
    task_id: str,
    status: TaskStatus,
    retry_count: int,
    max_retries: int,
    progress: int = 0,
    task_type: str = TaskType.PROCESS_DOCUMENT.value,
) -> Task:
    task = Task(
        task_id=task_id,
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        file_id=file.id,
        task_type=task_type,
        status=status,
        retry_count=retry_count,
        max_retries=max_retries,
        progress=progress,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def test_task_retry_fields_failed_task_can_retry(db_session, test_user, test_workspace):
    file = _file(db_session, test_user, test_workspace)
    task = _task(
        db_session,
        test_user,
        test_workspace,
        file,
        task_id="failed-task",
        status=TaskStatus.FAILURE,
        retry_count=1,
        max_retries=3,
        progress=42,
    )

    fields = task_retry_fields(task)

    assert fields == {
        "task_id": task.id,
        "task_uuid": "failed-task",
        "task_status": "failure",
        "task_progress": 42,
        "retry_count": 1,
        "max_retries": 3,
        "remaining_retries": 2,
        "can_retry": True,
    }


def test_task_retry_fields_exhausted_task_cannot_retry(db_session, test_user, test_workspace):
    file = _file(db_session, test_user, test_workspace)
    task = _task(
        db_session,
        test_user,
        test_workspace,
        file,
        task_id="exhausted-task",
        status=TaskStatus.FAILURE,
        retry_count=3,
        max_retries=3,
    )

    fields = task_retry_fields(task)

    assert fields["remaining_retries"] == 0
    assert fields["can_retry"] is False


def test_document_processing_fields_uses_latest_process_document_task(
    db_session, test_user, test_workspace
):
    file = _file(db_session, test_user, test_workspace, ProcessingStatus.failed)
    _task(
        db_session,
        test_user,
        test_workspace,
        file,
        task_id="older-process-task",
        status=TaskStatus.FAILURE,
        retry_count=1,
        max_retries=3,
    )
    _task(
        db_session,
        test_user,
        test_workspace,
        file,
        task_id="newer-parse-task",
        status=TaskStatus.SUCCESS,
        retry_count=0,
        max_retries=3,
        task_type=TaskType.PARSE_DOCUMENT.value,
    )
    latest_task = _task(
        db_session,
        test_user,
        test_workspace,
        file,
        task_id="newer-process-task",
        status=TaskStatus.CANCELLED,
        retry_count=2,
        max_retries=3,
        progress=75,
    )

    fields = document_processing_fields(db_session, file)

    assert fields["processing_status"] == "failed"
    assert fields["processing_error"] is None
    assert fields["task_id"] == latest_task.id
    assert fields["task_uuid"] == "newer-process-task"
    assert fields["task_status"] == "cancelled"
    assert fields["remaining_retries"] == 1
    assert fields["can_retry"] is True


def test_document_conflict_detail_failed_file_is_retryable(
    db_session, test_user, test_workspace
):
    file = _file(db_session, test_user, test_workspace, ProcessingStatus.failed)
    file.processing_error = "parse failed"
    db_session.commit()
    task = _task(
        db_session,
        test_user,
        test_workspace,
        file,
        task_id="retryable-task",
        status=TaskStatus.FAILURE,
        retry_count=0,
        max_retries=2,
    )

    detail = document_conflict_detail(db_session, file, file.uri)

    assert detail["code"] == "file_already_exists_processing_failed"
    assert detail["message"] == "File already exists and previous processing failed"
    assert detail["document"] == {
        "id": file.id,
        "path": file.uri,
        "name": file.name,
        "processing_status": "failed",
        "processing_error": "parse failed",
        "task_id": task.id,
        "task_uuid": "retryable-task",
        "task_status": "failure",
        "task_progress": 0,
        "retry_count": 0,
        "max_retries": 2,
        "remaining_retries": 2,
        "can_retry": True,
    }


def test_document_conflict_detail_string_failed_file_is_retryable(
    db_session, test_user, test_workspace
):
    file = _file(db_session, test_user, test_workspace)
    file.processing_status = "failed"

    detail = document_conflict_detail(db_session, file, file.uri)

    assert detail["code"] == "file_already_exists_processing_failed"
    assert detail["message"] == "File already exists and previous processing failed"
    assert detail["document"]["processing_status"] == "failed"


def test_document_conflict_detail_non_failed_file_cannot_retry_even_with_failed_task(
    db_session, test_user, test_workspace
):
    file = _file(db_session, test_user, test_workspace, ProcessingStatus.completed)
    task = _task(
        db_session,
        test_user,
        test_workspace,
        file,
        task_id="stale-failed-task",
        status=TaskStatus.FAILURE,
        retry_count=0,
        max_retries=3,
    )

    detail = document_conflict_detail(db_session, file, file.uri)

    assert detail["code"] == "file_already_exists"
    assert detail["message"] == f"File already exists at {file.uri}"
    assert detail["document"]["processing_status"] == "completed"
    assert detail["document"]["task_id"] == task.id
    assert detail["document"]["remaining_retries"] == 3
    assert detail["document"]["can_retry"] is False
