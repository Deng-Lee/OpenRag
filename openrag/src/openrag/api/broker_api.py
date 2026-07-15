"""Broker API endpoints for task scheduling"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from openrag.api.deps import get_db
from openrag.broker import TaskBroker
from openrag.models.task import Task

router = APIRouter(prefix="/broker", tags=["broker"])


class TaskResponse(BaseModel):
    """Task response model"""
    id: int
    task_id: str
    workspace_id: int
    user_id: int
    file_id: Optional[int] = None
    task_type: str
    queue: str
    priority: int
    status: str
    progress: int
    retry_count: int
    max_retries: int
    created_at: str
    updated_at: str
    started_at: str
    completed_at: str
    result: Optional[dict] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_retryable: bool = False
    next_retry_at: Optional[str] = None
    payload: Optional[dict] = None

    class Config:
        from_attributes = True


class GetTasksResponse(BaseModel):
    """Get tasks response"""
    tasks: List[dict]
    total: int


class HeartbeatResponse(BaseModel):
    """Heartbeat response"""
    success: bool
    message: str


def get_broker(db: Session = Depends(get_db)) -> TaskBroker:
    """Dependency to get TaskBroker instance"""
    return TaskBroker(db)


@router.get("/get-tasks", response_model=GetTasksResponse)
async def get_tasks(
    worker_id: str = Query(..., description="Worker unique identifier"),
    limit: int = Query(1, ge=1, le=20, description="Number of tasks to fetch (default: 1)"),
    broker: TaskBroker = Depends(get_broker)
):
    """Get tasks for worker with dynamic weight-based allocation

    This endpoint allows workers to pull tasks from the broker.
    Tasks are allocated based on:
    1. Dynamic weights per workspace
    2. Minimum 1 task per workspace (fairness)
    3. Available quota based on workspace max_concurrent_tasks

    Args:
        worker_id: Unique identifier for the worker
        limit: Maximum number of tasks to return (default: 5)

    Returns:
        List of assigned tasks
    """
    try:
        tasks = broker.get_tasks(worker_id, limit)
        return GetTasksResponse(
            tasks=[task.to_dict() for task in tasks],
            total=len(tasks)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get tasks: {str(e)}")


@router.post("/heartbeat/{task_id}", response_model=HeartbeatResponse)
async def heartbeat(
    task_id: int,
    broker: TaskBroker = Depends(get_broker)
):
    """Update heartbeat for a task

    Workers should call this endpoint periodically (every 30 seconds)
    to indicate the task is still being processed.

    If a task doesn't receive a heartbeat for 10 minutes,
    it will be automatically recovered and reassigned.

    Args:
        task_id: Task ID to update

    Returns:
        Success status
    """
    try:
        broker.update_heartbeat(task_id)
        return HeartbeatResponse(success=True, message="Heartbeat updated")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update heartbeat: {str(e)}")


@router.post("/recover-timeout")
async def recover_timeout(
    broker: TaskBroker = Depends(get_broker)
):
    """Manually trigger timeout recovery

    This endpoint recovers tasks that haven't received a heartbeat
    for more than 10 minutes. Normally this is done automatically,
    but can be triggered manually for maintenance.

    Returns:
        Number of recovered tasks
    """
    try:
        count = broker.recover_timeout_tasks()
        return {"recovered_count": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to recover tasks: {str(e)}")


@router.get("/weights")
async def get_weights(
    broker: TaskBroker = Depends(get_broker)
):
    """Get current dynamic weights for all workspaces

    This is useful for monitoring the broker's scheduling decisions.

    Returns:
        Dict of workspace weights
    """
    try:
        weights = broker.get_workspace_weights()
        return {"weights": weights}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get weights: {str(e)}")
