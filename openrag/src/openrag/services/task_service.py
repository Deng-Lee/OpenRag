"""Task management service"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from openrag.models.task import Task, TaskStatus
from openrag.models.workspace import Workspace


class TaskService:
    """Service for managing tasks"""

    def __init__(self, db: Session):
        self.db = db

    def add_task(
        self,
        workspace_id: int,
        user_id: int,
        file_id: Optional[int] = None,
        task_type: str = "process_document",
        queue: str = "normal",
        priority: int = 5,
        max_retries: int = 3,
        status: Optional[TaskStatus] = None,
        payload: Optional[Dict[str, Any]] = None,
        index_generation_id: Optional[str] = None,
    ) -> Task:
        """Build + add a Task to the session WITHOUT committing.

        Lets a caller include task creation in a larger single-commit transaction
        (e.g. soft-delete: set deleted_at + tag=None + enqueue cleanup, one commit).
        """
        raw_status = status if status else TaskStatus.PENDING
        status_val = (
            raw_status.value if isinstance(raw_status, TaskStatus) else raw_status
        )
        task = Task(
            task_id=str(uuid4()),
            workspace_id=workspace_id,
            user_id=user_id,
            file_id=file_id,
            task_type=task_type,
            queue=queue,
            priority=priority,
            status=status_val,
            progress=0,
            retry_count=0,
            max_retries=max_retries,
            payload=payload,
            index_generation_id=index_generation_id,
        )
        self.db.add(task)
        return task

    def create_task(
        self,
        workspace_id: int,
        user_id: int,
        file_id: Optional[int] = None,
        task_type: str = "process_document",
        queue: str = "normal",
        priority: int = 5,
        max_retries: int = 3,
        status: Optional[TaskStatus] = None,
        payload: Optional[Dict[str, Any]] = None,
        index_generation_id: Optional[str] = None,
    ) -> Task:
        """Create a new task and commit (commit-on-call contract, unchanged)."""
        task = self.add_task(
            workspace_id=workspace_id,
            user_id=user_id,
            file_id=file_id,
            task_type=task_type,
            queue=queue,
            priority=priority,
            max_retries=max_retries,
            status=status,
            payload=payload,
            index_generation_id=index_generation_id,
        )
        self.db.commit()
        self.db.refresh(task)
        return task

    def get_task(self, task_id: int) -> Optional[Task]:
        """Get task by ID

        Args:
            task_id: Task ID

        Returns:
            Task if found, None otherwise
        """
        return self.db.query(Task).filter(Task.id == task_id).first()

    def get_task_by_celery_id(self, celery_task_id: str) -> Optional[Task]:
        """Get task by Celery task ID

        Args:
            celery_task_id: Celery task UUID

        Returns:
            Task if found, None otherwise
        """
        return self.db.query(Task).filter(Task.task_id == celery_task_id).first()

    def list_tasks(
        self,
        workspace_id: Optional[int] = None,
        user_id: Optional[int] = None,
        status: Optional[TaskStatus] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Tuple[List[Task], int]:
        """List tasks with filtering

        Args:
            workspace_id: Filter by workspace
            user_id: Filter by user
            status: Filter by status
            skip: Number of records to skip
            limit: Maximum number of records

        Returns:
            Tuple of (tasks list, total count)
        """
        query = self.db.query(Task)

        if workspace_id is not None:
            query = query.filter(Task.workspace_id == workspace_id)

        if user_id is not None:
            query = query.filter(Task.user_id == user_id)

        if status is not None:
            query = query.filter(Task.status == status)

        # Get total count
        total = query.count()

        # Apply pagination
        tasks = query.order_by(Task.created_at.desc()).offset(skip).limit(limit).all()

        return tasks, total

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        error_retryable: Optional[bool] = None,
    ) -> Optional[Task]:
        """Update task status

        Args:
            task_id: Task ID
            status: New status
            progress: Progress percentage (0-100)
            result: Task result data
            error: Error message

        Returns:
            Updated task if found, None otherwise
        """
        task = self.get_task(task_id)
        if not task:
            return None

        task.status = status

        if progress is not None:
            task.progress = max(task.progress, max(0, min(100, progress)))

        if status == TaskStatus.STARTED and not task.started_at:
            task.started_at = datetime.now(timezone.utc)

        if status in (TaskStatus.SUCCESS, TaskStatus.FAILURE, TaskStatus.CANCELLED):
            task.completed_at = datetime.now(timezone.utc)

        if result is not None:
            task.result = result

        if error is not None:
            task.error = error
        if error_code is not None:
            task.error_code = error_code
        if error_retryable is not None:
            task.error_retryable = error_retryable
        if status == TaskStatus.SUCCESS:
            task.error = None
            task.error_code = None
            task.error_retryable = False
            task.next_retry_at = None

        self.db.commit()
        self.db.refresh(task)

        return task

    def schedule_task_retry(
        self,
        task_id: int,
        *,
        error: str,
        error_code: str,
        delay_seconds: int,
    ) -> Optional[Task]:
        task = self.get_task(task_id)
        if not task or task.retry_count >= task.max_retries:
            return None
        task.status = TaskStatus.RETRY.value
        task.retry_count += 1
        task.progress = 0
        task.error = error
        task.error_code = error_code
        task.error_retryable = True
        task.next_retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=delay_seconds
        )
        task.worker_id = None
        task.assigned_at = None
        task.heartbeat_at = None
        task.completed_at = None
        self.db.commit()
        self.db.refresh(task)
        return task

    def update_task_progress(self, task_id: int, progress: int) -> Optional[Task]:
        """Update task progress

        Args:
            task_id: Task ID
            progress: Progress percentage (0-100)

        Returns:
            Updated task if found, None otherwise
        """
        task = self.get_task(task_id)
        if not task:
            return None

        task.progress = max(task.progress, max(0, min(100, progress)))
        self.db.commit()
        self.db.refresh(task)

        return task

    def cancel_task(self, task_id: int) -> Optional[Task]:
        """Cancel a pending or running task

        Args:
            task_id: Task ID

        Returns:
            Cancelled task if found and cancelled, None otherwise
        """
        task = self.get_task(task_id)
        if not task:
            return None

        # Can only cancel pending or running tasks
        if task.status not in (
            TaskStatus.PENDING,
            TaskStatus.STARTED,
            TaskStatus.RETRY,
        ):
            return None

        task.status = TaskStatus.CANCELLED
        task.completed_at = datetime.now(timezone.utc)
        task.progress = 0

        self.db.commit()
        self.db.refresh(task)

        return task

    def retry_task(self, task_id: int) -> Optional[Task]:
        """Retry a failed task

        Args:
            task_id: Task ID

        Returns:
            Task if retry initiated, None otherwise
        """
        task = self.get_task(task_id)
        if not task:
            return None

        # Can only retry failed or cancelled tasks
        if task.status not in (TaskStatus.FAILURE, TaskStatus.CANCELLED):
            return None

        # Check if max retries reached
        if task.retry_count >= task.max_retries:
            return None

        task.status = TaskStatus.PENDING
        task.retry_count += 1
        task.progress = 0
        task.completed_at = None
        task.error = None
        task.error_code = None
        task.error_retryable = False
        task.next_retry_at = None

        self.db.commit()
        self.db.refresh(task)

        return task

    def get_task_stats(self, workspace_id: Optional[int] = None) -> Dict[str, Any]:
        """Get task statistics

        Args:
            workspace_id: Filter by workspace (optional)

        Returns:
            Statistics dictionary
        """
        query = self.db.query(Task)

        if workspace_id is not None:
            query = query.filter(Task.workspace_id == workspace_id)

        # Count by status
        status_counts = {}
        for status in TaskStatus:
            count = query.filter(Task.status == status).count()
            status_counts[status.value] = count

        # Total count
        total = sum(status_counts.values())

        # Running tasks count (including assigned and started)
        running = status_counts.get(TaskStatus.ASSIGNED.value, 0) + status_counts.get(
            TaskStatus.STARTED.value, 0
        )

        # Pending tasks count
        pending = status_counts.get(TaskStatus.PENDING.value, 0)

        return {
            "total": total,
            "running": running,
            "pending": pending,
            "by_status": status_counts,
        }

    def get_workspace_running_count(self, workspace_id: int) -> int:
        """Get number of running tasks in a workspace

        Args:
            workspace_id: Workspace ID

        Returns:
            Number of running tasks
        """
        return (
            self.db.query(Task)
            .filter(
                Task.workspace_id == workspace_id,
                Task.status.in_(
                    [
                        TaskStatus.PENDING.value,
                        TaskStatus.STARTED.value,
                        TaskStatus.RETRY.value,
                    ]
                ),
            )
            .count()
        )

    def delete_task(self, task_id: int) -> bool:
        """Delete a task

        Args:
            task_id: Task ID

        Returns:
            True if deleted, False if not found
        """
        task = self.get_task(task_id)
        if not task:
            return False

        self.db.delete(task)
        self.db.commit()

        return True

    def cleanup_old_tasks(self, days: int = 30) -> int:
        """Clean up old completed tasks

        Note: This is for maintenance purposes. Tasks are kept permanently
        by default, but can be cleaned up if needed.

        Args:
            days: Delete tasks older than this many days

        Returns:
            Number of tasks deleted
        """
        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff = cutoff.replace(day=cutoff.day - days)

        result = (
            self.db.query(Task)
            .filter(
                Task.status.in_([TaskStatus.SUCCESS.value, TaskStatus.CANCELLED.value]),
                Task.completed_at < cutoff,
            )
            .delete(synchronize_session=False)
        )

        self.db.commit()

        return result
