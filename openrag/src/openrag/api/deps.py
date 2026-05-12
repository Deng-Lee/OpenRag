"""Dependency injection for FastAPI endpoints"""

from typing import Generator, Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from openrag.api.auth import (
    CredentialsException,
    InactiveUserException,
    decode_and_validate_token,
    extract_user_id_from_payload,
)
from openrag.database import get_engine
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.role import Role, RoleWorkspacePermission, UserRole
from openrag.services.service_token_service import (
    ServiceTokenContext,
    resolve_service_token_context,
)

# OAuth2 scheme for token extraction
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def get_db() -> Generator[Session, None, None]:
    """
    Get database session dependency.

    Yields:
        Database session

    Note:
        Session is automatically closed after request completes
    """
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_service_token_context(
    db: Session = Depends(get_db),
    x_openrag_token: Optional[str] = Header(default=None, alias="X-OpenRag-Token"),
) -> ServiceTokenContext:
    """Resolve ``X-OpenRag-Token`` to a :class:`ServiceTokenContext` (service API routes)."""
    return resolve_service_token_context(db, x_openrag_token)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    """
    Get current authenticated user from JWT token.

    Args:
        token: JWT token from Authorization header
        db: Database session

    Returns:
        Current authenticated user

    Raises:
        HTTPException: If token is invalid or user not found
    """
    # Decode and validate token
    payload = decode_and_validate_token(token)

    # Extract user ID
    user_id = extract_user_id_from_payload(payload)

    # Query user from database
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Get current active user (checks if user account is active).

    Args:
        current_user: Current authenticated user

    Returns:
        Current active user

    Raises:
        HTTPException: If user account is inactive
    """
    if not current_user.is_active:
        raise InactiveUserException()

    return current_user


async def get_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Workspace:
    """Get workspace and verify user access (direct member or role-based)"""
    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # System admin has access to all workspaces
    if current_user.is_admin:
        return workspace

    # Check direct membership
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == current_user.id
    ).first()

    if member:
        return workspace

    # Check role-based access
    role_access = (
        db.query(RoleWorkspacePermission)
        .join(Role, RoleWorkspacePermission.role_id == Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(
            RoleWorkspacePermission.workspace_id == workspace_id,
            UserRole.user_id == current_user.id,
            Role.is_active == True,
        )
        .first()
    )

    if role_access:
        return workspace

    raise HTTPException(status_code=403, detail="Not a member of this workspace")


async def get_workspace_admin(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Workspace:
    """Get workspace and verify user has write permission (direct member, role-based, or system admin)"""
    workspace = await get_workspace(workspace_id, current_user, db)

    # System admin has all permissions
    if current_user.is_admin:
        return workspace

    # Check direct membership for write permission
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == current_user.id
    ).first()

    if member and member.role in ('write', 'admin'):
        return workspace

    # Check role-based write permission
    role_perm = (
        db.query(RoleWorkspacePermission.permission)
        .join(Role, RoleWorkspacePermission.role_id == Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(
            RoleWorkspacePermission.workspace_id == workspace_id,
            UserRole.user_id == current_user.id,
            Role.is_active == True,
        )
        .first()
    )

    if role_perm and role_perm[0] == 'write':
        return workspace

    raise HTTPException(status_code=403, detail="Write permission required")
