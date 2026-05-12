"""Share API endpoints"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.models.file import File
from openrag.models.share import ShareLink
from openrag.models.user import User
from openrag.services.share_manager import ShareLinkManager
from openrag.services.workspace_service import WorkspaceService


router = APIRouter(prefix="/share", tags=["sharing"])


# Pydantic schemas
class ShareLinkResponse(BaseModel):
    """Share link response schema"""
    id: int
    file_id: int
    token: str
    password_protected: bool
    expires_at: Optional[str] = None
    max_access_count: Optional[int] = None
    access_count: int
    created_at: str

    model_config = {"from_attributes": True}


class CreateShareLinkRequest(BaseModel):
    """Create share link request"""
    file_id: int = Field(..., description="File ID to share")
    password: Optional[str] = Field(None, description="Optional password for protection")
    expires_at: Optional[datetime] = Field(None, description="Optional expiration timestamp")
    max_access_count: Optional[int] = Field(None, description="Optional maximum access count")


class FileInfoResponse(BaseModel):
    """File info response for share access"""
    id: int
    name: str
    size: int
    mime_type: Optional[str]
    created_at: str


class ShareAccessResponse(BaseModel):
    """Share access response"""
    file: FileInfoResponse
    access_count: int


class MessageResponse(BaseModel):
    """Generic message response"""
    message: str


@router.post("/links", response_model=ShareLinkResponse, status_code=status.HTTP_201_CREATED)
async def create_share_link(
    request: CreateShareLinkRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Create a share link for a file

    Args:
        request: Create share link request
        current_user: Current authenticated user
        db: Database session

    Returns:
        Created share link
    """
    # Get file
    file = db.query(File).filter(File.id == request.file_id).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found"
        )

    # Check if user has read permission to the file's workspace
    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(file.workspace_id, current_user.id, 'read'):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to share this file"
        )

    # Create share link
    share_manager = ShareLinkManager(db)
    try:
        share_link = share_manager.create_share_link(
            file_id=request.file_id,
            created_by=current_user.id,
            password=request.password,
            expires_at=request.expires_at,
            max_access_count=request.max_access_count
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    return ShareLinkResponse(
        id=share_link.id,
        file_id=share_link.file_id,
        token=share_link.token,
        password_protected=share_link.password_hash is not None,
        expires_at=share_link.expires_at.isoformat() if share_link.expires_at else None,
        max_access_count=share_link.max_access_count,
        access_count=share_link.access_count,
        created_at=share_link.created_at.isoformat()
    )


@router.get("/links", response_model=list[ShareLinkResponse])
async def list_share_links(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    List all share links created by current user

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of share links
    """
    share_links = db.query(ShareLink).filter(
        ShareLink.created_by == current_user.id
    ).all()

    return [
        ShareLinkResponse(
            id=link.id,
            file_id=link.file_id,
            token=link.token,
            password_protected=link.password_hash is not None,
            expires_at=link.expires_at.isoformat() if link.expires_at else None,
            max_access_count=link.max_access_count,
            access_count=link.access_count,
            created_at=link.created_at.isoformat()
        )
        for link in share_links
    ]


@router.get("/links/{link_id}", response_model=ShareLinkResponse)
async def get_share_link(
    link_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get share link details (creator only)

    Args:
        link_id: Share link ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Share link details
    """
    share_link = db.query(ShareLink).filter(ShareLink.id == link_id).first()
    if not share_link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share link not found"
        )

    # Check if user is the creator
    if share_link.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this share link"
        )

    return ShareLinkResponse(
        id=share_link.id,
        file_id=share_link.file_id,
        token=share_link.token,
        password_protected=share_link.password_hash is not None,
        expires_at=share_link.expires_at.isoformat() if share_link.expires_at else None,
        max_access_count=share_link.max_access_count,
        access_count=share_link.access_count,
        created_at=share_link.created_at.isoformat()
    )


@router.delete("/links/{link_id}", response_model=MessageResponse)
async def delete_share_link(
    link_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Delete share link (creator only)

    Args:
        link_id: Share link ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Success message
    """
    share_link = db.query(ShareLink).filter(ShareLink.id == link_id).first()
    if not share_link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share link not found"
        )

    # Check if user is the creator
    if share_link.created_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete this share link"
        )

    db.delete(share_link)
    db.commit()

    return MessageResponse(message="Share link deleted successfully")


@router.get("/{token}", response_model=ShareAccessResponse)
async def access_share_link(
    token: str,
    password: Optional[str] = Query(None, description="Password for protected links"),
    db: Session = Depends(get_db)
):
    """
    Access file via share link (no authentication required)

    Args:
        token: Share link token
        password: Optional password for protected links
        db: Database session

    Returns:
        File information
    """
    share_manager = ShareLinkManager(db)
    file, error = share_manager.access_share_link(token, password)

    if error:
        if error == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Share link not found"
            )
        elif error == "expired":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Share link has expired"
            )
        elif error == "max_access_reached":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Share link has reached maximum access count"
            )
        elif error == "invalid_password":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid password"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Access denied: {error}"
            )

    # Get updated share link for access count
    share_link = share_manager.get_share_link(token)

    return ShareAccessResponse(
        file=FileInfoResponse(
            id=file.id,
            name=file.name,
            size=file.size,
            mime_type=file.mime_type,
            created_at=file.created_at.isoformat()
        ),
        access_count=share_link.access_count
    )
