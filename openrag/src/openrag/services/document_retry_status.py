from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from openrag.models.file import File, ProcessingStatus
from openrag.models.task import Task, TaskStatus, TaskType
from openrag.services.task_service import TaskService


def _status_value(status: Any) -> Any:
    return status.value if hasattr(status, "value") else status


def latest_process_document_task(db: Session, file_id: int) -> Task | None:
    return (
        db.query(Task)
        .filter(Task.file_id == file_id, Task.task_type == TaskType.PROCESS_DOCUMENT.value)
        .order_by(Task.id.desc())
        .first()
    )


def task_retry_fields(task: Task | None) -> dict[str, Any]:
    if task is None:
        return {
            "task_id": None,
            "task_uuid": None,
            "task_status": None,
            "task_progress": None,
            "retry_count": 0,
            "max_retries": 0,
            "remaining_retries": 0,
            "can_retry": False,
        }

    remaining_retries = max(task.max_retries - task.retry_count, 0)
    return {
        "task_id": task.id,
        "task_uuid": task.task_id,
        "task_status": _status_value(task.status),
        "task_progress": task.progress,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "remaining_retries": remaining_retries,
        "can_retry": task.status in (TaskStatus.FAILURE, TaskStatus.CANCELLED)
        and remaining_retries > 0,
    }


def document_processing_fields(db: Session, file: File) -> dict[str, Any]:
    fields = {
        "processing_status": _status_value(file.processing_status),
        "processing_error": file.processing_error,
    }
    fields.update(task_retry_fields(latest_process_document_task(db, file.id)))
    return fields


def retry_failed_document_processing(db: Session, file: File) -> Task:
    """Retry latest failed/cancelled process_document task for a file."""
    task = latest_process_document_task(db, file.id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No process_document task to retry",
        )
    if task.status not in (TaskStatus.FAILURE, TaskStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is not in a retryable state",
        )
    if task.retry_count >= task.max_retries:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Retry limit reached",
        )

    retry_task = TaskService(db).retry_task(task.id)
    if retry_task is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document is not in a retryable state",
        )
    file.processing_status = ProcessingStatus.pending
    file.processing_error = None
    db.commit()
    db.refresh(file)
    db.refresh(retry_task)
    return retry_task


def document_conflict_detail(db: Session, file: File, file_uri: str) -> dict[str, Any]:
    is_failed = _status_value(file.processing_status) == ProcessingStatus.failed.value
    if is_failed:
        code = "file_already_exists_processing_failed"
        message = "File already exists and previous processing failed"
    else:
        code = "file_already_exists"
        message = f"File already exists at {file_uri}"

    processing_fields = document_processing_fields(db, file)
    if not is_failed:
        processing_fields["can_retry"] = False

    return {
        "code": code,
        "message": message,
        "document": {
            "id": file.id,
            "path": file.uri,
            "name": file.name,
            **processing_fields,
        },
    }
