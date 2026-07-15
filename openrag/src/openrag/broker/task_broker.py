"""Task Broker for dynamic weight-based task scheduling

This module provides a pure embedded task broker that:
1. Manages task queues per workspace
2. Calculates dynamic weights for fair scheduling
3. Handles task assignment with database row locking
4. Recovers timeout tasks automatically
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy import and_, func, or_, update
from sqlalchemy.orm import Session
import threading
import time

from openrag.models.task import Task, TaskStatus
from openrag.models.workspace import Workspace


class TaskBroker:
    """Task broker for dynamic weight-based scheduling

    This is a pure embedded broker that runs within the application process.
    It uses database row locking (SELECT ... FOR UPDATE SKIP LOCKED) to ensure
    concurrent safety across multiple workers.

    Key features:
    - Dynamic weight calculation per workspace
    - Guaranteed minimum allocation (at least 1 task per workspace)
    - Automatic weight decay and recovery
    - Timeout detection and recovery
    """

    def __init__(self, db: Session):
        self.db = db
        # In-memory cache for dynamic weights: {workspace_id: current_weight}
        self._dynamic_weights: Dict[int, float] = {}
        self._lock = threading.Lock()

    def get_tasks(self, worker_id: str, limit: int = 1) -> List[Task]:
        """Get tasks for worker with dynamic weight-based allocation

        Args:
            worker_id: Unique identifier for the worker
            limit: Maximum number of tasks to return (default: 1)

        Returns:
            List of assigned tasks
        """
        # Step 1: Recover timeout tasks (10 minutes no heartbeat)
        self._recover_timeout_tasks()

        # Step 2: Count active workers (unique worker_ids with recent heartbeat)
        #   This ensures total ASSIGNED/STARTED tasks never exceed active worker count,
        #   so each worker only ever holds 1 task at a time.
        active_workers = self._count_active_workers()
        global_limit = max(1, active_workers)

        # Step 3: Calculate quotas based on dynamic weights, capped by global limit
        quotas = self._calculate_dynamic_quotas(min(limit, global_limit))

        # Step 4: Assign tasks according to quotas
        return self._assign_tasks(worker_id, quotas)

    def _count_active_workers(self) -> int:
        """Count unique workers with heartbeat in the last 15 minutes."""
        threshold = datetime.now() - timedelta(minutes=15)
        result = self.db.query(
            func.count(func.distinct(Task.worker_id))
        ).filter(
            Task.status.in_([TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value]),
            Task.heartbeat_at >= threshold,
            Task.worker_id.isnot(None),
        ).scalar()
        return result or 1

    def update_heartbeat(self, task_id: int) -> None:
        """Update heartbeat timestamp for a task

        Args:
            task_id: Task ID to update
        """
        self.db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(heartbeat_at=func.now())
        )
        self.db.commit()

    def recover_timeout_tasks(self) -> int:
        """Recover tasks that haven't updated heartbeat for 10 minutes

        Returns:
            Number of recovered tasks
        """
        return self._recover_timeout_tasks()

    def _recover_timeout_tasks(self) -> int:
        """Internal method to recover timeout tasks within a transaction"""
        timeout_threshold = datetime.now() - timedelta(minutes=10)

        result = self.db.execute(
            update(Task)
            .where(
                Task.status.in_([TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value]),
                Task.heartbeat_at < timeout_threshold
            )
            .values(
                status=TaskStatus.PENDING.value,
                worker_id=None,
                assigned_at=None,
                progress=0
            )
        )

        return result.rowcount

    def _calculate_dynamic_quotas(self, total: int) -> Dict[int, int]:
        """Calculate task quotas per workspace using dynamic weights

        Enforces workspace max_concurrent_tasks: if a workspace already has
        N tasks in ASSIGNED/STARTED status, it can only receive
        (max_concurrent_tasks - N) more tasks.

        Args:
            total: Total number of tasks to allocate

        Returns:
            Dict mapping workspace_id to quota
        """
        # Get workspaces with pending tasks
        workspace_stats = self.db.query(
            Task.workspace_id,
            func.count(Task.id).label('pending_count')
        ).filter(
            self._ready_task_filter()
        ).group_by(Task.workspace_id).all()

        if not workspace_stats:
            with self._lock:
                self._dynamic_weights.clear()
            return {}

        workspace_ids = [ws.workspace_id for ws in workspace_stats]

        # Get workspace configurations (base weights from priority_strategy)
        workspaces = self.db.query(Workspace).filter(
            Workspace.id.in_(workspace_ids)
        ).all()
        ws_max = {ws.id: ws.max_concurrent_tasks for ws in workspaces}

        # Count currently ASSIGNED/STARTED tasks per workspace
        active_stats = self.db.query(
            Task.workspace_id,
            func.count(Task.id).label('active_count')
        ).filter(
            Task.status.in_([TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value])
        ).group_by(Task.workspace_id).all()
        ws_active = {s.workspace_id: s.active_count for s in active_stats}

        # Compute available slots per workspace
        ws_available = {}
        for ws_id in workspace_ids:
            max_c = ws_max.get(ws_id, 10)
            active = ws_active.get(ws_id, 0)
            ws_available[ws_id] = max(0, max_c - active)

        # Initialize dynamic weights if not exists
        with self._lock:
            for ws in workspaces:
                if ws.id not in self._dynamic_weights:
                    self._dynamic_weights[ws.id] = float(ws.max_concurrent_tasks)

        # Check if all weights are near 0 (need reset)
        with self._lock:
            all_near_zero = all(
                w < 0.01 for w in self._dynamic_weights.values()
            )
            if all_near_zero:
                for ws in workspaces:
                    self._dynamic_weights[ws.id] = float(ws.max_concurrent_tasks)

        # Calculate quotas — respect total limit AND per-workspace available slots
        quotas = {}
        remaining = total

        for ws_stat in workspace_stats:
            if remaining <= 0:
                break
            avail = ws_available.get(ws_stat.workspace_id, 0)
            if avail <= 0 or ws_stat.pending_count <= 0:
                continue
            allocate = min(1, avail, remaining)
            quotas[ws_stat.workspace_id] = allocate
            remaining -= allocate

        # Distribute remaining quota by dynamic weight proportion (also capped by available slots)
        if remaining > 0:
            with self._lock:
                eligible_ws = [ws for ws in workspaces
                               if ws.id in quotas and ws_available.get(ws.id, 0) > quotas.get(ws.id, 0)]
                total_weight = sum(
                    self._dynamic_weights.get(ws.id, 0) for ws in eligible_ws
                )
                if total_weight > 0:
                    for ws in eligible_ws:
                        weight = self._dynamic_weights.get(ws.id, 0)
                        extra_quota = int(remaining * weight / total_weight)
                        avail = ws_available.get(ws.id, 0)
                        extra = min(extra_quota, avail - quotas.get(ws.id, 0))
                        if extra > 0:
                            quotas[ws.id] += extra
                            remaining -= extra

        # Apply weight decay
        with self._lock:
            for ws in workspaces:
                if ws.id in quotas and quotas[ws.id] > 0:
                    self._dynamic_weights[ws.id] *= 0.8

        return quotas

    def _assign_tasks(self, worker_id: str, quotas: Dict[int, int]) -> List[Task]:
        """Assign tasks to worker according to quotas

        Uses SELECT ... FOR UPDATE SKIP LOCKED to ensure atomicity
        and prevent multiple workers from getting the same task.

        Args:
            worker_id: Worker identifier
            quotas: Dict mapping workspace_id to quota

        Returns:
            List of assigned tasks
        """
        assigned_tasks = []
        now = datetime.now()

        for workspace_id, quota in quotas.items():
            if quota <= 0:
                continue

            # Use SKIP LOCKED to avoid blocking and ensure only one worker gets the task
            # Must query and update in the same transaction for locking to work
            tasks = self.db.query(Task).filter(
                Task.workspace_id == workspace_id,
                self._ready_task_filter()
            ).with_for_update(
                skip_locked=True
            ).limit(quota).all()

            for task in tasks:
                task.status = TaskStatus.ASSIGNED.value
                task.worker_id = worker_id
                task.assigned_at = now
                task.heartbeat_at = now
                assigned_tasks.append(task)

        # Commit immediately to release locks
        self.db.commit()

        return assigned_tasks

    def get_workspace_weights(self) -> Dict[int, Dict]:
        """Get current dynamic weights for monitoring

        Returns:
            Dict with workspace weights info
        """
        with self._lock:
            return {
                ws_id: {
                    "dynamic_weight": weight,
                    "base_weight": self.db.query(Workspace.max_concurrent_tasks)
                    .filter(Workspace.id == ws_id).scalar()
                }
                for ws_id, weight in self._dynamic_weights.items()
            }
    @staticmethod
    def _ready_task_filter():
        return or_(
            Task.status == TaskStatus.PENDING.value,
            and_(
                Task.status == TaskStatus.RETRY.value,
                Task.next_retry_at <= func.now(),
            ),
        )
