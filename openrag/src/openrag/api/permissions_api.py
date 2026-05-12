"""Permissions API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.models.file import File
from openrag.models.permission import EntityType, FilePermission, Permission
from openrag.models.user import User
from openrag.services.permission_manager import PermissionManager


router = APIRouter(prefix="/files", tags=["permissions"])


# Pydantic schemas
class PermissionResponse(BaseModel):
    """Permission response schema"""

    id: int
    file_id: int
    entity_type: str
    entity_id: int
    permission: str
    created_at: str

    model_config = {"from_attributes": True}


class GrantPermissionRequest(BaseModel):
    """Grant permission request"""

    entity_type: str = Field(..., description="Entity type (user or team)")
    entity_id: int = Field(..., description="User ID or Team ID")
    permission: str = Field(..., description="Permission level (read, write, admin)")


class MessageResponse(BaseModel):
    """Generic message response"""

    message: str


@router.get("/{file_id}/permissions", response_model=list[PermissionResponse])
async def list_permissions(
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List all permissions for a file (owner only)

    Args:
        file_id: File ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of permissions
    """
    # Get file
    file = db.query(File).filter(File.id == file_id).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check if user is file owner
    if file.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the file owner can view permissions",
        )

    # Get permissions
    perm_manager = PermissionManager(db)
    permissions = perm_manager.get_file_permissions(file_id)

    # Convert to response format
    return [
        PermissionResponse(
            id=p.id,
            file_id=p.file_id,
            entity_type=p.entity_type.value,
            entity_id=p.entity_id,
            permission=p.permission.value,
            created_at=p.created_at.isoformat(),
        )
        for p in permissions
    ]


@router.post(
    "/{file_id}/permissions",
    response_model=PermissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_permission(
    file_id: int,
    request: GrantPermissionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Grant permission to user or team (owner only)

    Args:
        file_id: File ID
        request: Grant permission request
        current_user: Current authenticated user
        db: Database session

    Returns:
        Created permission
    """
    # Get file
    file = db.query(File).filter(File.id == file_id).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check if user is file owner
    if file.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the file owner can grant permissions",
        )

    # Validate entity_type
    try:
        entity_type = EntityType(request.entity_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid entity_type: {request.entity_type}. Must be 'user' or 'team'",
        )

    # Validate permission
    if request.permission not in ["read", "write", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid permission: {request.permission}. Must be 'read', 'write', or 'admin'",
        )

    # Grant permission
    perm_manager = PermissionManager(db)
    try:
        permission = perm_manager.grant_permission(
            file_id=file_id,
            entity_type=entity_type,
            entity_id=request.entity_id,
            permission=request.permission,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return PermissionResponse(
        id=permission.id,
        file_id=permission.file_id,
        entity_type=permission.entity_type.value,
        entity_id=permission.entity_id,
        permission=permission.permission.value,
        created_at=permission.created_at.isoformat(),
    )


@router.delete("/{file_id}/permissions/{permission_id}", response_model=MessageResponse)
async def revoke_permission(
    file_id: int,
    permission_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Revoke permission (owner only)

    Args:
        file_id: File ID
        permission_id: Permission ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Success message
    """
    # Get file
    file = db.query(File).filter(File.id == file_id).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check if user is file owner
    if file.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the file owner can revoke permissions",
        )

    # Get permission
    permission = (
        db.query(FilePermission)
        .filter(FilePermission.id == permission_id, FilePermission.file_id == file_id)
        .first()
    )

    if not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Permission not found"
        )

    # Delete permission
    db.delete(permission)
    db.commit()

    return MessageResponse(message="Permission revoked successfully")


from openrag.models.role import Role, RoleWorkspacePermission, UserRole
from openrag.models.workspace import Workspace, WorkspaceMember

user_permissions_router = APIRouter(prefix="/users", tags=["permissions"])


@user_permissions_router.get("/{user_id}/permissions/details")
async def get_user_permission_details(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id != user_id and not current_user.is_admin:
        raise HTTPException(
            status_code=403, detail="Not authorized to view these permissions"
        )

    from sqlalchemy import select

    roles = db.scalars(
        select(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    ).all()

    direct_perms = db.scalars(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    ).all()

    role_perms = db.execute(
        select(RoleWorkspacePermission, Role.name, Workspace.name)
        .join(Role, RoleWorkspacePermission.role_id == Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .join(Workspace, RoleWorkspacePermission.workspace_id == Workspace.id)
        .where(UserRole.user_id == user_id, Role.is_active == True)
    ).all()

    workspace_permissions = []

    for dp in direct_perms:
        ws = db.scalar(select(Workspace).where(Workspace.id == dp.workspace_id))
        workspace_permissions.append(
            {
                "workspace_id": dp.workspace_id,
                "workspace_name": ws.name if ws else "Unknown",
                "permission": dp.role,
                "source": "direct",
                "source_details": None,
            }
        )

    for rp, role_name, ws_name in role_perms:
        workspace_permissions.append(
            {
                "workspace_id": rp.workspace_id,
                "workspace_name": ws_name,
                "permission": rp.permission,
                "source": f"role:{role_name}",
                "source_details": {"role_id": rp.role_id, "role_name": role_name},
            }
        )

    return {
        "user_id": user_id,
        "roles": [
            {
                "id": r.id,
                "name": r.name,
                "role_code": r.role_code,
                "is_active": r.is_active,
            }
            for r in roles
        ],
        "workspace_permissions": workspace_permissions,
    }
