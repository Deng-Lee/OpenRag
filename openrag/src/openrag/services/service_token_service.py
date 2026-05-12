"""Resolve machine credentials from the X-OpenRag-Token header."""

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from openrag.models.service_token import ServiceToken
from openrag.models.service_token_workspace import ServiceTokenWorkspace
from openrag.models.workspace import Workspace

_READ = "read"
_WRITE = "write"


@dataclass(frozen=True)
class TokenWorkspaceBinding:
    workspace_id: int
    permission: str


@dataclass(frozen=True)
class ServiceTokenContext:
    token_id: int
    bindings: list[TokenWorkspaceBinding]


def resolve_service_token_context(db: Session, raw_token: str | None) -> ServiceTokenContext:
    if raw_token is None or not raw_token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service token",
        )
    token_value = raw_token.strip()
    if not token_value.startswith("sk-"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service token",
        )

    row = (
        db.query(ServiceToken)
        .filter(
            ServiceToken.secret == token_value,
            ServiceToken.revoked_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service token",
        )

    bindings = db.query(ServiceTokenWorkspace).filter_by(token_id=row.id).all()
    if not bindings:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token has no workspace bindings",
        )

    return ServiceTokenContext(
        token_id=row.id,
        bindings=[
            TokenWorkspaceBinding(
                workspace_id=b.workspace_id,
                permission=b.permission if b.permission in (_READ, _WRITE) else _READ,
            )
            for b in bindings
        ],
    )


def require_workspace_for_name(db: Session, workspace_name: str) -> Workspace:
    key = (workspace_name or "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )
    ws = db.query(Workspace).filter(Workspace.name == key).first()
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )
    return ws


def assert_token_workspace_permission(
    ctx: ServiceTokenContext,
    workspace_id: int,
    need: Literal["read", "write"],
) -> None:
    binding = next((b for b in ctx.bindings if b.workspace_id == workspace_id), None)
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token not authorized for this workspace",
        )
    if need == "read":
        if binding.permission not in (_READ, _WRITE):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token permission insufficient",
            )
        return
    if need == "write":
        if binding.permission != _WRITE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token permission insufficient",
            )
        return