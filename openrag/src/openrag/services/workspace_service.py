"""Workspace service for business logic"""

from typing import List, Optional
from sqlalchemy.orm import Session
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User


class WorkspaceService:
    """Service for workspace operations"""

    def __init__(self, db: Session):
        self.db = db

    def create_workspace(
        self,
        name: str,
        slug: str,
        description: Optional[str],
        owner_id: int,
        max_concurrent_tasks: int = 10,
        max_storage_bytes: int = 10 * 1024 * 1024 * 1024,
        priority_strategy: str = "file_size",
    ) -> Workspace:
        """Create a new workspace and add owner as admin member"""
        workspace = Workspace(
            name=name,
            slug=slug,
            description=description,
            owner_id=owner_id,
            max_concurrent_tasks=max_concurrent_tasks,
            max_storage_bytes=max_storage_bytes,
            priority_strategy=priority_strategy,
        )
        self.db.add(workspace)
        self.db.flush()  # Get workspace.id

        # Add owner as admin member
        member = WorkspaceMember(
            workspace_id=workspace.id, user_id=owner_id, role="admin"
        )
        self.db.add(member)
        self.db.commit()
        self.db.refresh(workspace)

        return workspace

    def get_workspace(self, workspace_id: int) -> Optional[Workspace]:
        """Get workspace by ID"""
        return self.db.query(Workspace).filter(Workspace.id == workspace_id).first()

    def get_user_workspaces(self, user_id: int) -> List[Workspace]:
        """Get all workspaces user is a member of (direct or via role)"""
        from openrag.models.role import Role, RoleWorkspacePermission, UserRole

        direct_ws = (
            self.db.query(Workspace)
            .join(WorkspaceMember, Workspace.id == WorkspaceMember.workspace_id)
            .filter(WorkspaceMember.user_id == user_id)
        )

        role_ws = (
            self.db.query(Workspace)
            .join(
                RoleWorkspacePermission,
                Workspace.id == RoleWorkspacePermission.workspace_id,
            )
            .join(Role, RoleWorkspacePermission.role_id == Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(UserRole.user_id == user_id, Role.is_active == True)
        )

        return direct_ws.union(role_ws).all()

    def add_member(
        self, workspace_id: int, user_id: int, role: str = "member"
    ) -> WorkspaceMember:
        """Add a member to workspace"""
        member = WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role)
        self.db.add(member)
        self.db.commit()
        self.db.refresh(member)
        return member

    def remove_member(self, workspace_id: int, user_id: int) -> bool:
        """Remove a member from workspace"""
        member = (
            self.db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )

        if member:
            self.db.delete(member)
            self.db.commit()
            return True
        return False

    def get_workspace_members(self, workspace_id: int) -> List[WorkspaceMember]:
        """Get all members of a workspace"""
        return (
            self.db.query(WorkspaceMember)
            .filter(WorkspaceMember.workspace_id == workspace_id)
            .all()
        )

    def check_user_access(self, workspace_id: int, user_id: int) -> bool:
        """Check if user has access to workspace (is a member or has a role)"""
        from openrag.models.role import Role, RoleWorkspacePermission, UserRole

        member = (
            self.db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )

        if member is not None:
            return True

        role_access = (
            self.db.query(RoleWorkspacePermission)
            .join(Role, RoleWorkspacePermission.role_id == Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(
                RoleWorkspacePermission.workspace_id == workspace_id,
                UserRole.user_id == user_id,
                Role.is_active == True,
            )
            .first()
        )

        return role_access is not None

    def check_user_permission(
        self, workspace_id: int, user_id: int, required_permission: str
    ) -> bool:
        """
        Check if user has required permission for workspace

        Args:
            workspace_id: Workspace ID
            user_id: User ID
            required_permission: 'read' or 'write'

        Returns:
            True if user has permission, False otherwise
        """
        from openrag.models.user import User
        from openrag.models.role import Role, RoleWorkspacePermission, UserRole

        # Admin has all permissions
        user = self.db.query(User).filter(User.id == user_id).first()
        if user and user.is_admin:
            return True

        permissions = set()

        # Check workspace membership
        member = (
            self.db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )

        if member:
            permissions.add(member.role)

        # Check role permissions
        role_perms = (
            self.db.query(RoleWorkspacePermission.permission)
            .join(Role, RoleWorkspacePermission.role_id == Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(
                RoleWorkspacePermission.workspace_id == workspace_id,
                UserRole.user_id == user_id,
                Role.is_active == True,
            )
            .all()
        )

        for rp in role_perms:
            permissions.add(rp[0])

        if required_permission == "read":
            return (
                "read" in permissions
                or "write" in permissions
                or "admin" in permissions
            )
        elif required_permission == "write":
            return "write" in permissions or "admin" in permissions

        return False

    def get_user_workspaces_with_permission(
        self, user_id: int, permission: str = "read"
    ) -> List[Workspace]:
        """
        Get all workspaces user has specific permission for

        Args:
            user_id: User ID
            permission: 'read' or 'write'

        Returns:
            List of Workspace objects
        """
        from openrag.models.user import User
        from openrag.models.role import Role, RoleWorkspacePermission, UserRole

        # Admin can see all workspaces
        user = self.db.query(User).filter(User.id == user_id).first()
        if user and user.is_admin:
            return self.db.query(Workspace).all()

        # Direct permissions
        direct_query = (
            self.db.query(Workspace)
            .join(WorkspaceMember, Workspace.id == WorkspaceMember.workspace_id)
            .filter(WorkspaceMember.user_id == user_id)
        )

        if permission == "write":
            direct_query = direct_query.filter(WorkspaceMember.role == "write")

        # Role permissions
        role_query = (
            self.db.query(Workspace)
            .join(
                RoleWorkspacePermission,
                Workspace.id == RoleWorkspacePermission.workspace_id,
            )
            .join(Role, RoleWorkspacePermission.role_id == Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(
                UserRole.user_id == user_id,
                Role.is_active == True,
            )
        )

        if permission == "write":
            role_query = role_query.filter(
                RoleWorkspacePermission.permission == "write"
            )

        return direct_query.union(role_query).all()

    def update_workspace_quota(
        self,
        workspace_id: int,
        max_concurrent_tasks: Optional[int] = None,
        max_storage_bytes: Optional[int] = None,
        priority_strategy: Optional[str] = None,
    ) -> Workspace:
        """Update workspace quota configuration"""
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")

        if max_concurrent_tasks is not None:
            workspace.max_concurrent_tasks = max_concurrent_tasks
        if max_storage_bytes is not None:
            workspace.max_storage_bytes = max_storage_bytes
        if priority_strategy is not None:
            workspace.priority_strategy = priority_strategy

        self.db.commit()
        self.db.refresh(workspace)
        return workspace

    def delete_workspace(self, workspace_id: int) -> bool:
        """Delete a workspace (cascade deletes members)"""
        workspace = self.get_workspace(workspace_id)
        if workspace:
            self.db.delete(workspace)
            self.db.commit()
            return True
        return False
