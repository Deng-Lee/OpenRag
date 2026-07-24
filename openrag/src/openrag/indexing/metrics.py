"""Current-state gauges derived from authoritative rows, not retry event counters."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from openrag.models.index_generation import IndexGeneration
from openrag.models.task import Task, TaskStatus, TaskType


def collect_generation_progress_metrics(db: Session) -> list[dict]:
    return [
        {
            "generation_id": item.id,
            "state": item.state,
            "expected_files": item.expected_file_count,
            "indexed_files": item.indexed_file_count,
            "failed_files": item.failed_file_count,
            "build_lag_files": item.build_lag_files,
        }
        for item in db.query(IndexGeneration).all()
    ]


def collect_previous_lag_metrics(db: Session) -> dict[str, int]:
    return {
        item.id: item.mirror_lag_files
        for item in db.query(IndexGeneration)
        .filter(IndexGeneration.mirror_lag_files > 0)
        .all()
    }


def collect_cleanup_failure_metrics(db: Session) -> list[dict]:
    rows = (
        db.query(Task)
        .filter(
            Task.task_type.in_(
                [TaskType.DELETE_FILE.value, TaskType.DELETE_PATH_PREFIX.value, TaskType.PURGE_FILE_FROM_GENERATIONS.value]
            ),
            Task.status.in_([TaskStatus.RETRY.value, TaskStatus.FAILURE.value]),
        )
        .all()
    )
    return [
        {
            "task_id": item.id,
            "status": item.status,
            "error_code": item.error_code,
            "failed_generation_ids": (item.result or {}).get("failed_generation_ids", []),
        }
        for item in rows
    ]


def collect_task_state_gauges(db: Session) -> list[dict]:
    rows = db.query(Task.task_type, Task.status, func.count(Task.id)).group_by(Task.task_type, Task.status).all()
    return [
        {"task_type": task_type, "status": status, "count": count}
        for task_type, status, count in rows
    ]
