"""Permission management service - handles ACL permission checking and hierarchical inheritance"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.models.file import File
from openrag.models.permission import EntityType, FilePermission, Permission
from openrag.models.team import Team, TeamMember
from openrag.models.user import User


class PermissionManager:
    """Permission management service for ACL permission checking and hierarchical inheritance"""

    # Permission hierarchy mapping
    PERMISSION_HIERARCHY = {
        "admin": ["admin", "write", "read"],
        "write": ["write", "read"],
        "read": ["read"]
    }

    def __init__(self, db: Session):
        """
        Initialize PermissionManager with database session

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def grant_permission(
        self,
        file_id: int,
        entity_type: EntityType,
        entity_id: int,
        permission: str
    ) -> FilePermission:
        """
        Grant permission to a user or team for a file

        Args:
            file_id: File ID
            entity_type: Entity type (user or team)
            entity_id: User ID or Team ID
            permission: Permission level (read, write, admin)

        Returns:
            Created FilePermission object

        Raises:
            ValueError: If file/entity doesn't exist, permission is invalid, or duplicate permission
        """
        # Validate permission
        if permission not in ["read", "write", "admin"]:
            raise ValueError(f"Invalid permission: {permission}. Must be one of: read, write, admin")

        # Validate file exists
        file = self.db.execute(
            select(File).where(File.id == file_id)
        ).scalar_one_or_none()

        if not file:
            raise ValueError(f"File with id {file_id} not found")

        # Validate entity exists
        if entity_type == EntityType.USER:
            user = self.db.execute(
                select(User).where(User.id == entity_id)
            ).scalar_one_or_none()

            if not user:
                raise ValueError(f"User with id {entity_id} not found")
        elif entity_type == EntityType.TEAM:
            team = self.db.execute(
                select(Team).where(Team.id == entity_id)
            ).scalar_one_or_none()

            if not team:
                raise ValueError(f"Team with id {entity_id} not found")

        # Check for duplicate permissions
        existing = self.db.execute(
            select(FilePermission).where(
                FilePermission.file_id == file_id,
                FilePermission.entity_type == entity_type,
                FilePermission.entity_id == entity_id
            )
        ).scalar_one_or_none()

        if existing:
            raise ValueError(
                f"Permission already exists for {entity_type.value} {entity_id} on file {file_id}"
            )

        # Create permission
        perm = FilePermission(
            file_id=file_id,
            entity_type=entity_type,
            entity_id=entity_id,
            permission=Permission(permission)
        )

        self.db.add(perm)
        self.db.commit()
        self.db.refresh(perm)
        return perm

    def revoke_permission(
        self,
        file_id: int,
        entity_type: EntityType,
        entity_id: int
    ) -> bool:
        """
        Revoke permission from a user or team for a file

        Args:
            file_id: File ID
            entity_type: Entity type (user or team)
            entity_id: User ID or Team ID

        Returns:
            True if deleted, False if not found
        """
        perm = self.db.execute(
            select(FilePermission).where(
                FilePermission.file_id == file_id,
                FilePermission.entity_type == entity_type,
                FilePermission.entity_id == entity_id
            )
        ).scalar_one_or_none()

        if not perm:
            return False

        self.db.delete(perm)
        self.db.commit()
        return True

    def check_permission(
        self,
        user_id: int,
        file_id: int,
        required_permission: str
    ) -> bool:
        """
        Check if user has required permission for a file (with inheritance)

        Args:
            user_id: User ID
            file_id: File ID
            required_permission: Required permission level (read, write, admin)

        Returns:
            True if user has permission, False otherwise
        """
        # 1. Check if user is file owner (has all permissions)
        file = self.db.execute(
            select(File).where(File.id == file_id)
        ).scalar_one_or_none()

        if not file:
            return False

        if file.owner_id == user_id:
            return True

        # 2. Check direct user permissions on this file
        user_perm = self.db.execute(
            select(FilePermission).where(
                FilePermission.file_id == file_id,
                FilePermission.entity_type == EntityType.USER,
                FilePermission.entity_id == user_id
            )
        ).scalar_one_or_none()

        if user_perm and self._has_permission(user_perm.permission.value, required_permission):
            return True

        # 3. Check team permissions (user's teams) on this file
        team_perms = self.db.execute(
            select(FilePermission).join(
                TeamMember,
                (FilePermission.entity_id == TeamMember.team_id) &
                (FilePermission.entity_type == EntityType.TEAM)
            ).where(
                FilePermission.file_id == file_id,
                TeamMember.user_id == user_id
            )
        ).scalars().all()

        for team_perm in team_perms:
            if self._has_permission(team_perm.permission.value, required_permission):
                return True

        # 4. Check parent directory permissions (recursive up to root)
        if file.parent_id:
            return self.check_permission(user_id, file.parent_id, required_permission)

        return False

    def get_file_permissions(self, file_id: int) -> list[FilePermission]:
        """
        Get all permissions for a file

        Args:
            file_id: File ID

        Returns:
            List of FilePermission objects
        """
        perms = self.db.execute(
            select(FilePermission).where(FilePermission.file_id == file_id)
        ).scalars().all()

        return list(perms)

    def get_user_permissions(self, user_id: int, file_id: int) -> list[str]:
        """
        Get all permissions a user has for a file (direct + team + inherited)

        Args:
            user_id: User ID
            file_id: File ID

        Returns:
            List of permission strings (e.g., ["read", "write"])
        """
        permissions = set()

        # Check if owner
        file = self.db.execute(
            select(File).where(File.id == file_id)
        ).scalar_one_or_none()

        if file and file.owner_id == user_id:
            return ["read", "write", "admin"]

        # Get direct user permissions
        user_perm = self.db.execute(
            select(FilePermission).where(
                FilePermission.file_id == file_id,
                FilePermission.entity_type == EntityType.USER,
                FilePermission.entity_id == user_id
            )
        ).scalar_one_or_none()

        if user_perm:
            permissions.update(self.PERMISSION_HIERARCHY[user_perm.permission.value])

        # Get team permissions
        team_perms = self.db.execute(
            select(FilePermission).join(
                TeamMember,
                (FilePermission.entity_id == TeamMember.team_id) &
                (FilePermission.entity_type == EntityType.TEAM)
            ).where(
                FilePermission.file_id == file_id,
                TeamMember.user_id == user_id
            )
        ).scalars().all()

        for team_perm in team_perms:
            permissions.update(self.PERMISSION_HIERARCHY[team_perm.permission.value])

        # Get inherited permissions from parent
        if file and file.parent_id:
            parent_perms = self.get_user_permissions(user_id, file.parent_id)
            permissions.update(parent_perms)

        return sorted(list(permissions))

    def list_accessible_files(self, user_id: int) -> list[File]:
        """
        List all files a user can access (owned + shared + team)

        Args:
            user_id: User ID

        Returns:
            List of File objects user has any permission for
        """
        # Get owned files
        owned_files = self.db.execute(
            select(File).where(File.owner_id == user_id)
        ).scalars().all()

        # Get files with direct user permissions
        user_perm_files = self.db.execute(
            select(File).join(
                FilePermission,
                File.id == FilePermission.file_id
            ).where(
                FilePermission.entity_type == EntityType.USER,
                FilePermission.entity_id == user_id
            )
        ).scalars().all()

        # Get files with team permissions
        team_perm_files = self.db.execute(
            select(File).join(
                FilePermission,
                File.id == FilePermission.file_id
            ).join(
                TeamMember,
                (FilePermission.entity_id == TeamMember.team_id) &
                (FilePermission.entity_type == EntityType.TEAM)
            ).where(
                TeamMember.user_id == user_id
            )
        ).scalars().all()

        # Combine and deduplicate
        all_files = list(owned_files) + list(user_perm_files) + list(team_perm_files)
        unique_files = {f.id: f for f in all_files}

        return list(unique_files.values())

    def _has_permission(self, granted_permission: str, required_permission: str) -> bool:
        """
        Check if granted permission satisfies required permission

        Args:
            granted_permission: Permission that was granted
            required_permission: Permission that is required

        Returns:
            True if granted permission satisfies required permission
        """
        return required_permission in self.PERMISSION_HIERARCHY.get(granted_permission, [])
