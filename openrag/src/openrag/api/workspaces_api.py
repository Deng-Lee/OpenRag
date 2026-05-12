"""Workspace management API endpoints"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db, get_workspace, get_workspace_admin
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.role import Role, RoleWorkspacePermission, UserRole
from openrag.services.workspace_service import WorkspaceService


router = APIRouter(prefix="/workspaces", tags=["workspaces"])


# Pydantic schemas
class WorkspaceCreate(BaseModel):
    """Workspace creation request"""
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z0-9-]+$')
    description: Optional[str] = None
    max_concurrent_tasks: int = Field(default=10, ge=1, le=100)
    max_storage_bytes: int = Field(default=10*1024*1024*1024, ge=0)
    priority_strategy: str = Field(default='file_size')


class WorkspaceUpdate(BaseModel):
    """Workspace update request"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None


class WorkspaceQuotaUpdate(BaseModel):
    """Workspace quota update request"""
    max_concurrent_tasks: Optional[int] = Field(None, ge=1, le=100)
    max_storage_bytes: Optional[int] = Field(None, ge=0)
    priority_strategy: Optional[str] = None


class WorkspaceResponse(BaseModel):
    """Workspace response schema"""
    id: int
    name: str
    slug: str
    description: Optional[str]
    owner_id: int
    max_concurrent_tasks: int
    max_storage_bytes: int
    priority_strategy: str
    created_at: str
    updated_at: Optional[str]
    user_role: Optional[str] = None  # 当前用户在此工作空间的权限 (read/write)

    model_config = {"from_attributes": True}


class MemberAdd(BaseModel):
    """Add member request"""
    user_id: int
    role: str = Field(default='read', pattern=r'^(read|write)$')


class MemberResponse(BaseModel):
    """Member response schema"""
    id: int
    workspace_id: int
    user_id: int
    role: str
    joined_at: str

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    """Generic message response"""
    message: str


# Workspace endpoints
@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    request: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new workspace (admin only)"""
    # Only admin can create workspaces
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can create workspaces"
        )

    service = WorkspaceService(db)

    # Check if slug already exists
    existing = db.query(Workspace).filter(Workspace.slug == request.slug).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Workspace with slug '{request.slug}' already exists"
        )

    workspace = service.create_workspace(
        name=request.name,
        slug=request.slug,
        description=request.description,
        owner_id=current_user.id,
        max_concurrent_tasks=request.max_concurrent_tasks,
        max_storage_bytes=request.max_storage_bytes,
        priority_strategy=request.priority_strategy
    )

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        owner_id=workspace.owner_id,
        max_concurrent_tasks=workspace.max_concurrent_tasks,
        max_storage_bytes=workspace.max_storage_bytes,
        priority_strategy=workspace.priority_strategy,
        created_at=workspace.created_at.isoformat(),
        updated_at=workspace.updated_at.isoformat() if workspace.updated_at else None
    )


@router.get("", response_model=List[WorkspaceResponse])
async def list_workspaces(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List user's accessible workspaces (user must be a member)"""
    service = WorkspaceService(db)
    # Only return workspaces where user is a member
    workspaces = service.get_user_workspaces_with_permission(current_user.id, permission='read')

    # Get user's effective role for each workspace (direct membership + role-based)
    user_roles = {}
    if not current_user.is_admin:
        # Direct membership roles
        memberships = db.query(WorkspaceMember).filter(
            WorkspaceMember.user_id == current_user.id
        ).all()
        user_roles = {m.workspace_id: m.role for m in memberships}

        # Role-based permissions
        role_perms = (
            db.query(RoleWorkspacePermission.workspace_id, RoleWorkspacePermission.permission)
            .join(Role, RoleWorkspacePermission.role_id == Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(
                UserRole.user_id == current_user.id,
                Role.is_active == True,
            )
            .all()
        )

        # Merge: take highest permission per workspace (admin > write > read)
        role_order = {'admin': 3, 'write': 2, 'read': 1}
        for ws_id, perm in role_perms:
            existing = user_roles.get(ws_id)
            if existing is None or role_order.get(perm, 0) > role_order.get(existing, 0):
                user_roles[ws_id] = perm

    return [
        WorkspaceResponse(
            id=ws.id,
            name=ws.name,
            slug=ws.slug,
            description=ws.description,
            owner_id=ws.owner_id,
            max_concurrent_tasks=ws.max_concurrent_tasks,
            max_storage_bytes=ws.max_storage_bytes,
            priority_strategy=ws.priority_strategy,
            created_at=ws.created_at.isoformat(),
            updated_at=ws.updated_at.isoformat() if ws.updated_at else None,
            user_role='write' if current_user.is_admin else user_roles.get(ws.id, 'read')
        )
        for ws in workspaces
    ]


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace_detail(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get workspace details"""
    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Compute user_role using the same logic as list_workspaces
    service = WorkspaceService(db)
    user_role = 'read'
    if current_user.is_admin:
        user_role = 'write'
    else:
        # Direct membership
        member = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == current_user.id
        ).first()
        if member:
            user_role = member.role

        # Role-based permissions (merge, take highest)
        role_perms = (
            db.query(RoleWorkspacePermission.permission)
            .join(Role, RoleWorkspacePermission.role_id == Role.id)
            .join(UserRole, UserRole.role_id == Role.id)
            .filter(
                RoleWorkspacePermission.workspace_id == workspace_id,
                UserRole.user_id == current_user.id,
                Role.is_active == True,
            )
            .all()
        )
        role_order = {'admin': 3, 'write': 2, 'read': 1}
        for rp in role_perms:
            perm = rp[0]
            if role_order.get(perm, 0) > role_order.get(user_role, 0):
                user_role = perm

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        owner_id=workspace.owner_id,
        max_concurrent_tasks=workspace.max_concurrent_tasks,
        max_storage_bytes=workspace.max_storage_bytes,
        priority_strategy=workspace.priority_strategy,
        created_at=workspace.created_at.isoformat(),
        updated_at=workspace.updated_at.isoformat() if workspace.updated_at else None,
        user_role=user_role
    )


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    request: WorkspaceUpdate,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Update workspace (admin only)"""
    if request.name is not None:
        workspace.name = request.name
    if request.description is not None:
        workspace.description = request.description

    db.commit()
    db.refresh(workspace)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        owner_id=workspace.owner_id,
        max_concurrent_tasks=workspace.max_concurrent_tasks,
        max_storage_bytes=workspace.max_storage_bytes,
        priority_strategy=workspace.priority_strategy,
        created_at=workspace.created_at.isoformat(),
        updated_at=workspace.updated_at.isoformat() if workspace.updated_at else None
    )


@router.delete("/{workspace_id}", response_model=MessageResponse)
async def delete_workspace(
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Delete workspace (admin only)"""
    service = WorkspaceService(db)
    service.delete_workspace(workspace.id)

    return MessageResponse(message="Workspace deleted successfully")


# Member endpoints
@router.post("/{workspace_id}/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED)
async def add_member(
    request: MemberAdd,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Add member to workspace (admin only)"""
    service = WorkspaceService(db)

    # Check if user exists
    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already a member
    existing = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == request.user_id
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is already a member"
        )

    member = service.add_member(workspace.id, request.user_id, request.role)

    return MemberResponse(
        id=member.id,
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        role=member.role,
        joined_at=member.joined_at.isoformat()
    )


@router.get("/{workspace_id}/members", response_model=List[MemberResponse])
async def list_members(
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db)
):
    """List workspace members"""
    service = WorkspaceService(db)
    members = service.get_workspace_members(workspace.id)

    return [
        MemberResponse(
            id=m.id,
            workspace_id=m.workspace_id,
            user_id=m.user_id,
            role=m.role,
            joined_at=m.joined_at.isoformat()
        )
        for m in members
    ]


@router.delete("/{workspace_id}/members/{user_id}", response_model=MessageResponse)
async def remove_member(
    user_id: int,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Remove member from workspace (admin only)"""
    service = WorkspaceService(db)

    # Cannot remove owner
    if user_id == workspace.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove workspace owner"
        )

    success = service.remove_member(workspace.id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Member not found")

    return MessageResponse(message="Member removed successfully")


# Quota endpoint
@router.put("/{workspace_id}/quota", response_model=WorkspaceResponse)
async def update_quota(
    request: WorkspaceQuotaUpdate,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Update workspace quota (admin only)"""
    service = WorkspaceService(db)

    updated = service.update_workspace_quota(
        workspace.id,
        max_concurrent_tasks=request.max_concurrent_tasks,
        max_storage_bytes=request.max_storage_bytes,
        priority_strategy=request.priority_strategy
    )

    return WorkspaceResponse(
        id=updated.id,
        name=updated.name,
        slug=updated.slug,
        description=updated.description,
        owner_id=updated.owner_id,
        max_concurrent_tasks=updated.max_concurrent_tasks,
        max_storage_bytes=updated.max_storage_bytes,
        priority_strategy=updated.priority_strategy,
        created_at=updated.created_at.isoformat(),
        updated_at=updated.updated_at.isoformat() if updated.updated_at else None
    )
