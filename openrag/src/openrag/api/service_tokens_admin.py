"""JWT-only admin API for machine service tokens (multi-workspace binding model)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.models.service_token import ServiceToken
from openrag.models.service_token_workspace import ServiceTokenWorkspace
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User

router = APIRouter(tags=["service-tokens"])


def _generate_secret() -> str:
    return "sk-" + secrets.token_urlsafe(32)


def _secret_preview(secret: str) -> str:
    if len(secret) < 8:
        return "sk-****"
    return "sk-****" + secret[-4:]


def _assert_can_manage_token(user: User, token: ServiceToken) -> None:
    if user.is_admin or token.created_by_user_id == user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not allowed to manage this service token",
    )


def _assert_user_has_write_or_admin_on_workspace(user: User, workspace_id: int, db: Session) -> None:
    if user.is_admin:
        return
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user.id,
    ).first()
    if member and member.role == "write":
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to authorize access to this workspace",
    )


class WorkspaceBindingRequest(BaseModel):
    workspace_id: int
    permission: str = Field(pattern=r"^(read|write)$")


class ServiceTokenCreateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    workspaces: List[WorkspaceBindingRequest] = Field(..., min_length=1)


class BindingUpdateItem(BaseModel):
    workspace_id: int
    permission: str = Field(pattern=r"^(read|write)$")


class BindingRemoveItem(BaseModel):
    workspace_id: int


class ServiceTokenPatchBindingsRequest(BaseModel):
    add: Optional[List[BindingUpdateItem]] = None
    update: Optional[List[BindingUpdateItem]] = None
    remove: Optional[List[BindingRemoveItem]] = None


class WorkspaceBindingResponse(BaseModel):
    workspace_id: int
    workspace_name: str
    permission: str


class ServiceTokenCreatedResponse(BaseModel):
    id: int
    secret: str
    name: Optional[str]
    created_at: str
    workspaces: List[WorkspaceBindingResponse]


class ServiceTokenListItem(BaseModel):
    id: int
    name: Optional[str]
    secret_preview: str
    revoked_at: Optional[str]
    created_by_user_id: int
    workspaces: List[WorkspaceBindingResponse]


class ServiceTokenSecretResponse(BaseModel):
    secret: str


class MessageResponse(BaseModel):
    message: str


def _build_binding_response(binding: ServiceTokenWorkspace, db: Session) -> WorkspaceBindingResponse:
    ws = db.query(Workspace).filter(Workspace.id == binding.workspace_id).first()
    return WorkspaceBindingResponse(
        workspace_id=binding.workspace_id,
        workspace_name=ws.name if ws else f"workspace-{binding.workspace_id}",
        permission=binding.permission,
    )


def _build_token_list_item(token: ServiceToken, db: Session) -> ServiceTokenListItem:
    bindings = db.query(ServiceTokenWorkspace).filter_by(token_id=token.id).all()
    return ServiceTokenListItem(
        id=token.id,
        name=token.name,
        secret_preview=_secret_preview(token.secret),
        revoked_at=token.revoked_at.isoformat() if token.revoked_at else None,
        created_by_user_id=token.created_by_user_id,
        workspaces=[_build_binding_response(b, db) for b in bindings],
    )


@router.post(
    "/service-tokens",
    response_model=ServiceTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_service_token(
    body: ServiceTokenCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceTokenCreatedResponse:
    ws_ids = [w.workspace_id for w in body.workspaces]
    if len(ws_ids) != len(set(ws_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate workspace binding",
        )

    for w in body.workspaces:
        _assert_user_has_write_or_admin_on_workspace(current_user, w.workspace_id, db)

    secret = _generate_secret()
    row = ServiceToken(
        secret=secret,
        name=body.name,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    for w in body.workspaces:
        binding = ServiceTokenWorkspace(
            token_id=row.id,
            workspace_id=w.workspace_id,
            permission=w.permission,
        )
        db.add(binding)
    db.commit()

    bindings = db.query(ServiceTokenWorkspace).filter_by(token_id=row.id).all()
    return ServiceTokenCreatedResponse(
        id=row.id,
        secret=secret,
        name=row.name,
        created_at=row.created_at.isoformat(),
        workspaces=[_build_binding_response(b, db) for b in bindings],
    )


@router.get(
    "/service-tokens",
    response_model=List[ServiceTokenListItem],
)
async def list_service_tokens(
    workspace_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ServiceTokenListItem]:
    if current_user.is_admin:
        if workspace_id is not None:
            binding_ids = db.query(ServiceTokenWorkspace.token_id).filter_by(
                workspace_id=workspace_id
            ).subquery()
            rows = db.query(ServiceToken).filter(ServiceToken.id.in_(binding_ids)).all()
        else:
            rows = db.query(ServiceToken).all()
    else:
        user_ws_ids = [
            m.workspace_id for m in db.query(WorkspaceMember).filter_by(user_id=current_user.id).all()
        ]
        if workspace_id is not None:
            if workspace_id not in user_ws_ids:
                rows = []
            else:
                binding_ids = db.query(ServiceTokenWorkspace.token_id).filter_by(
                    workspace_id=workspace_id
                ).subquery()
                rows = db.query(ServiceToken).filter(ServiceToken.id.in_(binding_ids)).all()
        else:
            binding_ids = db.query(ServiceTokenWorkspace.token_id).filter(
                ServiceTokenWorkspace.workspace_id.in_(user_ws_ids)
            ).subquery()
            rows = db.query(ServiceToken).filter(ServiceToken.id.in_(binding_ids)).all()

    return [_build_token_list_item(t, db) for t in rows]


@router.patch(
    "/service-tokens/{token_id}/workspaces",
    response_model=ServiceTokenListItem,
)
async def patch_token_bindings(
    token_id: int,
    body: ServiceTokenPatchBindingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceTokenListItem:
    token = db.query(ServiceToken).filter(ServiceToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service token not found")
    _assert_can_manage_token(current_user, token)

    if token.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify bindings on a revoked token",
        )

    # add
    for item in (body.add or []):
        _assert_user_has_write_or_admin_on_workspace(current_user, item.workspace_id, db)
        existing = db.query(ServiceTokenWorkspace).filter_by(
            token_id=token.id, workspace_id=item.workspace_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Token already has a binding for this workspace",
            )
        db.add(ServiceTokenWorkspace(
            token_id=token.id,
            workspace_id=item.workspace_id,
            permission=item.permission,
        ))

    # update
    for item in (body.update or []):
        _assert_user_has_write_or_admin_on_workspace(current_user, item.workspace_id, db)
        existing = db.query(ServiceTokenWorkspace).filter_by(
            token_id=token.id, workspace_id=item.workspace_id
        ).first()
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Binding not found for this workspace",
            )
        existing.permission = item.permission
        db.add(existing)

    # remove
    for item in (body.remove or []):
        existing = db.query(ServiceTokenWorkspace).filter_by(
            token_id=token.id, workspace_id=item.workspace_id
        ).first()
        if existing:
            db.delete(existing)

    db.commit()
    return _build_token_list_item(token, db)


@router.delete("/service-tokens/{token_id}", response_model=MessageResponse)
async def revoke_service_token(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    token = db.query(ServiceToken).filter(ServiceToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service token not found")
    _assert_can_manage_token(current_user, token)
    if token.revoked_at is not None:
        return MessageResponse(message="Token already revoked")
    token.revoked_at = datetime.now(timezone.utc)
    db.add(token)
    db.commit()
    return MessageResponse(message="Token revoked")


@router.get(
    "/service-tokens/{token_id}/secret",
    response_model=ServiceTokenSecretResponse,
)
async def get_service_token_secret(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceTokenSecretResponse:
    token = db.query(ServiceToken).filter(ServiceToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service token not found")
    _assert_can_manage_token(current_user, token)
    return ServiceTokenSecretResponse(secret=token.secret)