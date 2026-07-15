"""User permissions API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.models.role import Role, RoleWorkspacePermission, UserRole
from openrag.models.user import User
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
