"""Audit logging service for tracking operations"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.models.audit import AuditLog
from openrag.models.user import User


class AuditLogger:
    """Service for logging and querying audit logs"""

    def __init__(self, db: Session):
        self.db = db

    def log(
        self,
        user_id: int,
        action: str,
        resource_type: str,
        resource_id: int,
        details: str | None = None,
        ip_address: str | None = None
    ) -> AuditLog:
        """Log an operation"""
        # Validate user exists
        user = self.db.execute(
            select(User).where(User.id == user_id)
        ).scalar_one_or_none()

        if user is None:
            raise ValueError(f"User with id {user_id} does not exist")

        # Create AuditLog entry
        audit_log = AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address
        )

        self.db.add(audit_log)
        self.db.commit()
        self.db.refresh(audit_log)

        return audit_log

    def get_user_logs(
        self,
        user_id: int,
        limit: int = 100,
        offset: int = 0
    ) -> list[AuditLog]:
        """Get audit logs for a specific user"""
        stmt = (
            select(AuditLog)
            .where(AuditLog.user_id == user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = self.db.execute(stmt)
        return list(result.scalars().all())

    def get_resource_logs(
        self,
        resource_type: str,
        resource_id: int,
        limit: int = 100,
        offset: int = 0
    ) -> list[AuditLog]:
        """Get audit logs for a specific resource"""
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.resource_type == resource_type,
                AuditLog.resource_id == resource_id
            )
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = self.db.execute(stmt)
        return list(result.scalars().all())

    def get_logs_by_action(
        self,
        action: str,
        limit: int = 100,
        offset: int = 0
    ) -> list[AuditLog]:
        """Get audit logs by action type"""
        stmt = (
            select(AuditLog)
            .where(AuditLog.action == action)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = self.db.execute(stmt)
        return list(result.scalars().all())

    def get_logs_in_range(
        self,
        start_time: datetime,
        end_time: datetime,
        user_id: int | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[AuditLog]:
        """Get audit logs within a time range with optional filters"""
        stmt = select(AuditLog).where(
            AuditLog.created_at >= start_time,
            AuditLog.created_at <= end_time
        )

        # Apply optional filters
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)

        if action is not None:
            stmt = stmt.where(AuditLog.action == action)

        stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)

        result = self.db.execute(stmt)
        return list(result.scalars().all())
