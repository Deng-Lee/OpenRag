"""Task management API endpoints"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db, get_workspace
from openrag.models.task import Task, TaskStatus
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.task_service import TaskService

router = APIRouter(prefix="/workspaces/{workspace_id}/tasks", tags=["tasks"])


# Pydantic schemas
class TaskListResponse(BaseModel):
    """Task list response"""

    items: List[Dict[str, Any]]
    total: int
    skip: int
    limit: int


class TaskResponse(BaseModel):
    """Task detail response"""

    id: int
    task_id: str
    workspace_id: int
    user_id: int
    file_id: Optional[int]
    index_generation_id: Optional[str]
    task_type: str
    queue: str
    priority: int
    status: str
    progress: int
    retry_count: int
    max_retries: int
    created_at: str
    updated_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    error_code: Optional[str]
    error_retryable: bool
    next_retry_at: Optional[str]


class TaskStatsResponse(BaseModel):
    """Task statistics response"""

    total: int
    running: int
    pending: int
    by_status: Dict[str, int]


class MessageResponse(BaseModel):
    """Generic message response"""

    message: str


# Helper function to convert Task to response dict
def task_to_response(task: Task) -> Dict[str, Any]:
    """Convert Task model to response dictionary"""
    return {
        "id": task.id,
        "task_id": task.task_id,
        "workspace_id": task.workspace_id,
        "user_id": task.user_id,
        "file_id": task.file_id,
        "index_generation_id": task.index_generation_id,
        "task_type": task.task_type,
        "queue": task.queue,
        "priority": task.priority,
        "status": (
            task.status.value if isinstance(task.status, TaskStatus) else task.status
        ),
        "progress": task.progress,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "result": task.result,
        "error": task.error,
        "error_code": task.error_code,
        "error_retryable": task.error_retryable,
        "next_retry_at": task.next_retry_at.isoformat() if task.next_retry_at else None,
    }


# Task endpoints
@router.get("/stats", response_model=TaskStatsResponse)
async def get_task_stats(
    workspace_id: int,
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get task statistics for workspace"""
    service = TaskService(db)
    stats = service.get_task_stats(workspace_id=workspace_id)

    return TaskStatsResponse(
        total=stats["total"],
        running=stats["running"],
        pending=stats["pending"],
        by_status=stats["by_status"],
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    workspace_id: int,
    status: Optional[str] = Query(None, description="Filter by status"),
    user_id: Optional[int] = Query(None, description="Filter by user"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records"),
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List tasks in workspace

    Query parameters:
    - status: Filter by status (pending, started, success, failure, retry, cancelled)
    - user_id: Filter by user who submitted the task
    - skip: Pagination offset
    - limit: Pagination limit
    """
    service = TaskService(db)

    # Parse status filter
    status_filter = None
    if status:
        try:
            status_filter = TaskStatus(status.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status}",
            )

    tasks, total = service.list_tasks(
        workspace_id=workspace_id,
        user_id=user_id,
        status=status_filter,
        skip=skip,
        limit=limit,
    )

    return TaskListResponse(
        items=[task_to_response(task) for task in tasks],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task_detail(
    workspace_id: int,
    task_id: int,
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get task details"""
    service = TaskService(db)
    task = service.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    # Verify task belongs to this workspace
    if task.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found in this workspace",
        )

    return TaskResponse(**task_to_response(task))


@router.post("/{task_id}/cancel", response_model=MessageResponse)
async def cancel_task(
    workspace_id: int,
    task_id: int,
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending or running task

    Only the task owner or workspace admin can cancel a task.
    """
    service = TaskService(db)
    task = service.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    # Verify task belongs to this workspace
    if task.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found in this workspace",
        )

    # Check permission (task owner or workspace admin)
    if task.user_id != current_user.id:
        from openrag.models.workspace import WorkspaceMember

        membership = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == current_user.id,
            )
            .first()
        )

        if not membership or membership.role not in ("admin", "write"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only task owner or workspace member can cancel tasks",
            )

    cancelled = service.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task cannot be cancelled (may not be in pending/started state)",
        )

    return MessageResponse(message="Task cancelled successfully")


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(
    workspace_id: int,
    task_id: int,
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retry a failed task

    Only the task owner or workspace admin can retry a task.
    """
    service = TaskService(db)
    task = service.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    # Verify task belongs to this workspace
    if task.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found in this workspace",
        )

    # Check permission (task owner or workspace admin)
    if task.user_id != current_user.id:
        from openrag.models.workspace import WorkspaceMember

        membership = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == current_user.id,
            )
            .first()
        )

        if not membership or membership.role not in ("admin", "write"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only task owner or workspace member can retry tasks",
            )

    retried = service.retry_task(task_id)
    if not retried:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task cannot be retried (may not be in failed state or max retries reached)",
        )

    return TaskResponse(**task_to_response(retried))
