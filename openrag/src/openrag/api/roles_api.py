from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import List, Optional

from openrag.api.deps import get_db, get_current_user
from openrag.models.user import User
from openrag.models.role import Role, RoleWorkspacePermission, UserRole

router = APIRouter(prefix="/roles", tags=["Roles"])


def require_admin(user: User = Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


class RoleCreate(BaseModel):
    name: str
    role_code: str
    is_active: bool = True
    description: Optional[str] = None


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    role_code: Optional[str] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None


class WorkspacePermCreate(BaseModel):
    workspace_id: int
    permission: str


@router.get("/")
def list_roles(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    roles = db.scalars(select(Role)).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "role_code": r.role_code,
            "is_active": r.is_active,
            "description": r.description,
        }
        for r in roles
    ]


@router.post("/")
def create_role(
    data: RoleCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)
):
    existing = db.scalar(select(Role).where(Role.role_code == data.role_code))
    if existing:
        raise HTTPException(status_code=400, detail="Role code already exists")

    role = Role(**data.model_dump())
    db.add(role)
    db.commit()
    db.refresh(role)
    return {
        "id": role.id,
        "name": role.name,
        "role_code": role.role_code,
        "is_active": role.is_active,
    }


@router.put("/{role_id}")
def update_role(
    role_id: int,
    data: RoleUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    role = db.scalar(select(Role).where(Role.id == role_id))
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if data.role_code and data.role_code != role.role_code:
        existing = db.scalar(select(Role).where(Role.role_code == data.role_code))
        if existing:
            raise HTTPException(status_code=400, detail="Role code already exists")

    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(role, k, v)

    db.commit()
    db.refresh(role)
    return {
        "id": role.id,
        "name": role.name,
        "role_code": role.role_code,
        "is_active": role.is_active,
    }


@router.delete("/{role_id}")
def delete_role(
    role_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)
):
    role = db.scalar(select(Role).where(Role.id == role_id))
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    db.delete(role)
    db.commit()
    return {"message": "Role deleted"}


@router.get("/{role_id}/permissions")
def get_role_permissions(
    role_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)
):
    perms = db.scalars(
        select(RoleWorkspacePermission).where(
            RoleWorkspacePermission.role_id == role_id
        )
    ).all()
    return [
        {"id": p.id, "workspace_id": p.workspace_id, "permission": p.permission}
        for p in perms
    ]


@router.post("/{role_id}/permissions")
def set_role_permission(
    role_id: int,
    data: WorkspacePermCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    role = db.scalar(select(Role).where(Role.id == role_id))
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    existing = db.scalar(
        select(RoleWorkspacePermission).where(
            RoleWorkspacePermission.role_id == role_id,
            RoleWorkspacePermission.workspace_id == data.workspace_id,
        )
    )

    if existing:
        existing.permission = data.permission
    else:
        new_perm = RoleWorkspacePermission(
            role_id=role_id, workspace_id=data.workspace_id, permission=data.permission
        )
        db.add(new_perm)

    db.commit()
    return {"message": "Permission set successfully"}


@router.delete("/{role_id}/permissions/{workspace_id}")
def remove_role_permission(
    role_id: int,
    workspace_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    perm = db.scalar(
        select(RoleWorkspacePermission).where(
            RoleWorkspacePermission.role_id == role_id,
            RoleWorkspacePermission.workspace_id == workspace_id,
        )
    )
    if perm:
        db.delete(perm)
        db.commit()
    return {"message": "Permission removed"}
