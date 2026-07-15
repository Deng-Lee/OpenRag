"""Task management model for document processing"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, Text, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes, strip_pg_nul_in_json

if TYPE_CHECKING:
    from openrag.models.user import User
    from openrag.models.workspace import Workspace
    from openrag.models.file import File


class TaskStatus(str, enum.Enum):
    """Task processing status"""
    PENDING = "pending"       # 等待执行
    ASSIGNED = "assigned"     # 已分配给Worker
    STARTED = "started"       # 执行中
    SUCCESS = "success"       # 成功完成
    FAILURE = "failure"       # 失败
    RETRY = "retry"          # 重试中
    CANCELLED = "cancelled"   # 已取消


class TaskType(str, enum.Enum):
    """Task type enumeration"""
    PROCESS_DOCUMENT = "process_document"  # 文档处理
    PARSE_DOCUMENT = "parse_document"      # 文档解析
    BUILD_HIERARCHY = "build_hierarchy"    # 构建层级
    EMBED_DOCUMENT = "embed_document"      # 文档嵌入
    DELETE_FILE = "delete_file"            # 异步删除（存储 + 向量 + DB）
    DELETE_PATH_PREFIX = "delete_path_prefix"  # 按路径前缀级联删除（虚拟目录 + 文件）


class Task(Base, TimestampMixin):
    """Task model for tracking document processing jobs

    Stores task state permanently for audit and management purposes.
    """

    __tablename__ = "tasks"

    # Primary fields
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="Celery task ID (UUID)"
    )

    # Foreign keys
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Workspace ID"
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who submitted the task"
    )
    file_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Associated file ID"
    )

    # Task configuration
    task_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TaskType.PROCESS_DOCUMENT.value,
        comment="Task type"
    )
    queue: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="normal",
        comment="Queue name: fast, normal, slow"
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        comment="Priority score (0-10, higher is more important)"
    )

    # Task state
    status: Mapped[TaskStatus] = mapped_column(
        String(16),
        nullable=False,
        default=TaskStatus.PENDING.value,
        comment="Task status"
    )
    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Progress percentage (0-100)"
    )

    # Retry configuration
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Current retry count"
    )
    max_retries: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        comment="Maximum retry attempts"
    )

    # Timing
    assigned_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP,
        nullable=True,
        comment="Task assignment time"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP,
        nullable=True,
        comment="Task start time"
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP,
        nullable=True,
        comment="Task completion time"
    )
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP,
        nullable=True,
        comment="Last heartbeat timestamp"
    )

    # Worker tracking
    worker_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="Worker ID that is processing this task"
    )

    # Results
    result: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
        comment="Task result data"
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if failed"
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="Stable task error code"
    )
    error_retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP, nullable=True, comment="Earliest time a retry task may be assigned"
    )
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
        comment="任务扩展参数（如 delete_path_prefix 的 path）",
    )

    @validates("task_id", "task_type", "queue", "status", "worker_id", "error")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    @validates("result", "payload")
    def _strip_nul_in_json_fields(self, _key: str, value: object) -> object:
        if value is None:
            return None
        return strip_pg_nul_in_json(value)

    # Relationships
    workspace: Mapped["Workspace"] = relationship("Workspace", foreign_keys=[workspace_id])
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    file: Mapped[Optional["File"]] = relationship("File", foreign_keys=[file_id])

    # Indexes for common queries
    __table_args__ = (
        # Index for querying tasks by workspace and status
        Index("idx_task_workspace_status", "workspace_id", "status"),
        # Index for querying tasks by user
        Index("idx_task_user_id", "user_id"),
        # Index for querying running tasks
        Index("idx_task_running", "status", "started_at"),
        # Index for querying assigned tasks by worker
        Index("idx_task_worker", "worker_id", "status"),
        # Index for timeout detection
        Index("idx_task_heartbeat", "status", "heartbeat_at"),
        Index(
            "idx_task_ready_retry",
            "status",
            "next_retry_at",
            "priority",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return f"<Task(id={self.id}, task_id='{self.task_id}', status='{self.status}', workspace_id={self.workspace_id})>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert task to dictionary"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "file_id": self.file_id,
            "task_type": self.task_type,
            "queue": self.queue,
            "priority": self.priority,
            "status": self.status.value if isinstance(self.status, TaskStatus) else self.status,
            "progress": self.progress,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "worker_id": self.worker_id,
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
            "error_retryable": self.error_retryable,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "payload": self.payload,
        }
