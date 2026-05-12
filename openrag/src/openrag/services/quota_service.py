"""Quota management service with Redis caching"""

from typing import Optional

from sqlalchemy.orm import Session

from openrag.models.task import Task, TaskStatus
from openrag.models.workspace import Workspace


class QuotaExceededError(Exception):
    """Raised when workspace quota is exceeded"""
    pass


class QuotaService:
    """Service for managing workspace quotas"""

    def __init__(self, db: Session, redis_client=None):
        self.db = db
        self.redis = redis_client

    def _get_cache_key(self, workspace_id: int) -> str:
        """Generate Redis cache key for running tasks count"""
        return f"workspace:{workspace_id}:running_tasks"

    def get_running_tasks_count(self, workspace_id: int, use_cache: bool = True) -> int:
        """Get number of running tasks in a workspace

        Args:
            workspace_id: Workspace ID
            use_cache: Whether to use Redis cache

        Returns:
            Number of running tasks
        """
        # Try cache first if available
        if use_cache and self.redis:
            cache_key = self._get_cache_key(workspace_id)
            cached_count = self.redis.get(cache_key)
            if cached_count is not None:
                return int(cached_count)

        # Query from database
        count = self.db.query(Task).filter(
            Task.workspace_id == workspace_id,
            Task.status.in_([
                TaskStatus.PENDING.value,
                TaskStatus.STARTED.value,
                TaskStatus.RETRY.value
            ])
        ).count()

        # Update cache if available
        if self.redis:
            cache_key = self._get_cache_key(workspace_id)
            self.redis.setex(cache_key, 60, count)  # TTL 60 seconds

        return count

    def increment_running_tasks(self, workspace_id: int) -> int:
        """Increment running tasks count

        Args:
            workspace_id: Workspace ID

        Returns:
            New count
        """
        if self.redis:
            cache_key = self._get_cache_key(workspace_id)
            new_count = self.redis.incr(cache_key)
            self.redis.expire(cache_key, 300)  # 5 minute TTL
            return int(new_count)
        else:
            # Fallback to database count
            return self.get_running_tasks_count(workspace_id, use_cache=False)

    def decrement_running_tasks(self, workspace_id: int) -> int:
        """Decrement running tasks count

        Args:
            workspace_id: Workspace ID

        Returns:
            New count (minimum 0)
        """
        if self.redis:
            cache_key = self._get_cache_key(workspace_id)
            # Use lua script to ensure count doesn't go below 0
            lua_script = """
                local current = redis.call('get', KEYS[1])
                if current and tonumber(current) > 0 then
                    return redis.call('decr', KEYS[1])
                else
                    return 0
                end
            """
            new_count = self.redis.eval(lua_script, 1, cache_key)
            self.redis.expire(cache_key, 300)  # 5 minute TTL
            return int(new_count)
        else:
            # Fallback to database count
            return self.get_running_tasks_count(workspace_id, use_cache=False)

    def check_workspace_quota(self, workspace_id: int) -> bool:
        """Check if workspace has reached concurrent task limit

        Args:
            workspace_id: Workspace ID

        Returns:
            True if quota available, raises QuotaExceededError otherwise

        Raises:
            QuotaExceededError: If quota exceeded
        """
        # Get workspace
        workspace = self.db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")

        # Get current running tasks count
        running_tasks = self.get_running_tasks_count(workspace_id)

        # Check if exceeded
        if running_tasks >= workspace.max_concurrent_tasks:
            raise QuotaExceededError(
                f"Workspace {workspace_id} reached max concurrent tasks "
                f"({running_tasks}/{workspace.max_concurrent_tasks})"
            )

        return True

    def get_quota_usage(self, workspace_id: int) -> dict:
        """Get quota usage for workspace

        Args:
            workspace_id: Workspace ID

        Returns:
            Dictionary with quota usage info
        """
        workspace = self.db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")

        running_tasks = self.get_running_tasks_count(workspace_id)
        pending_tasks = self.db.query(Task).filter(
            Task.workspace_id == workspace_id,
            Task.status == TaskStatus.PENDING.value
        ).count()

        return {
            "workspace_id": workspace_id,
            "max_concurrent_tasks": workspace.max_concurrent_tasks,
            "running_tasks": running_tasks,
            "pending_tasks": pending_tasks,
            "available_slots": max(0, workspace.max_concurrent_tasks - running_tasks),
            "usage_percentage": (
                (running_tasks / workspace.max_concurrent_tasks * 100)
                if workspace.max_concurrent_tasks > 0 else 0
            ),
        }

    def acquire_quota_slot(self, workspace_id: int) -> bool:
        """Try to acquire a quota slot for task execution

        Args:
            workspace_id: Workspace ID

        Returns:
            True if slot acquired, False otherwise

        Raises:
            QuotaExceededError: If quota exceeded
        """
        # Check quota
        self.check_workspace_quota(workspace_id)

        # Increment running count
        self.increment_running_tasks(workspace_id)

        return True

    def release_quota_slot(self, workspace_id: int) -> None:
        """Release a quota slot after task completion

        Args:
            workspace_id: Workspace ID
        """
        self.decrement_running_tasks(workspace_id)

    def reset_cache(self, workspace_id: int) -> None:
        """Reset cache for workspace

        Args:
            workspace_id: Workspace ID
        """
        if self.redis:
            cache_key = self._get_cache_key(workspace_id)
            self.redis.delete(cache_key)
