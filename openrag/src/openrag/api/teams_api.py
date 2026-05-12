"""Team Management API - CRUD Operations and Member Management"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_db, get_current_active_user
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.user import User
from openrag.services.team_manager import TeamManager


router = APIRouter(prefix="/teams", tags=["teams"])


# Pydantic Models
class TeamCreateRequest(BaseModel):
    """Team creation request"""
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., max_length=512)


class TeamUpdateRequest(BaseModel):
    """Team update request"""
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=512)


class TeamResponse(BaseModel):
    """Team response model"""
    id: int
    name: str
    description: str
    owner_id: int

    model_config = {"from_attributes": True}


class AddMemberRequest(BaseModel):
    """Add team member request"""
    user_id: int
    role: str = Field(..., pattern="^(owner|admin|member)$")


class TeamMemberResponse(BaseModel):
    """Team member response model"""
    id: int
    team_id: int
    user_id: int
    role: str

    model_config = {"from_attributes": True}


# Helper Functions
def check_team_owner(team: Team, user_id: int):
    """
    Check if user is team owner.

    Args:
        team: Team object
        user_id: User ID to check

    Raises:
        HTTPException: If user is not the team owner
    """
    if team.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only team owner can perform this action"
        )


def check_team_member(team_id: int, user_id: int, db: Session):
    """
    Check if user is a team member.

    Args:
        team_id: Team ID
        user_id: User ID to check
        db: Database session

    Raises:
        HTTPException: If user is not a team member
    """
    member = db.query(TeamMember).filter(
        TeamMember.team_id == team_id,
        TeamMember.user_id == user_id
    ).first()

    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this team"
        )


# API Endpoints
@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    request: TeamCreateRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Create a new team.

    Args:
        request: Team creation data
        current_user: Current authenticated user
        db: Database session

    Returns:
        Created team information

    Raises:
        HTTPException: If team name already exists
    """
    team_manager = TeamManager(db)

    try:
        team = team_manager.create_team(
            name=request.name,
            description=request.description,
            owner_id=current_user.id
        )
        return TeamResponse.model_validate(team)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("", response_model=List[TeamResponse])
async def list_teams(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    List all teams the current user is a member of.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of teams
    """
    team_manager = TeamManager(db)
    teams = team_manager.get_user_teams(current_user.id)
    return [TeamResponse.model_validate(team) for team in teams]


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Get team by ID.

    Args:
        team_id: Team ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Team information

    Raises:
        HTTPException: If team not found or user is not a member
    """
    team_manager = TeamManager(db)
    team = team_manager.get_team_by_id(team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )

    # Check if user is a member
    check_team_member(team_id, current_user.id, db)

    return TeamResponse.model_validate(team)


@router.put("/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: int,
    request: TeamUpdateRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Update team information.

    Args:
        team_id: Team ID
        request: Update data
        current_user: Current authenticated user
        db: Database session

    Returns:
        Updated team information

    Raises:
        HTTPException: If team not found, user is not owner, or name already exists
    """
    team_manager = TeamManager(db)
    team = team_manager.get_team_by_id(team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )

    # Check if user is owner
    check_team_owner(team, current_user.id)

    # Build update dict with only provided fields
    update_data = {}
    if request.name is not None:
        update_data["name"] = request.name
    if request.description is not None:
        update_data["description"] = request.description

    try:
        updated_team = team_manager.update_team(team_id, **update_data)
        if not updated_team:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Team not found"
            )
        return TeamResponse.model_validate(updated_team)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Delete team.

    Args:
        team_id: Team ID
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If team not found or user is not owner
    """
    team_manager = TeamManager(db)
    team = team_manager.get_team_by_id(team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )

    # Check if user is owner
    check_team_owner(team, current_user.id)

    try:
        team_manager.delete_team(team_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/{team_id}/members", response_model=TeamMemberResponse, status_code=status.HTTP_201_CREATED)
async def add_team_member(
    team_id: int,
    request: AddMemberRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Add a member to the team.

    Args:
        team_id: Team ID
        request: Member data
        current_user: Current authenticated user
        db: Database session

    Returns:
        Created team member information

    Raises:
        HTTPException: If team/user not found, user is not owner, or member already exists
    """
    team_manager = TeamManager(db)
    team = team_manager.get_team_by_id(team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )

    # Check if user is owner
    check_team_owner(team, current_user.id)

    # Check if target user exists
    from openrag.services.user_manager import UserManager
    user_manager = UserManager(db)
    target_user = user_manager.get_user_by_id(request.user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    try:
        # Convert string role to TeamRole enum
        role = TeamRole(request.role)
        member = team_manager.add_member(team_id, request.user_id, role)
        return TeamMemberResponse.model_validate(member)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.delete("/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    team_id: int,
    user_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Remove a member from the team.

    Args:
        team_id: Team ID
        user_id: User ID to remove
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If team not found, user is not owner, member not found, or trying to remove owner
    """
    team_manager = TeamManager(db)
    team = team_manager.get_team_by_id(team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )

    # Check if user is owner
    check_team_owner(team, current_user.id)

    # Prevent removing the owner
    if user_id == team.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove team owner from team"
        )

    # Remove member
    success = team_manager.remove_member(team_id, user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team member not found"
        )


@router.get("/{team_id}/members", response_model=List[TeamMemberResponse])
async def list_team_members(
    team_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    List all members of a team.

    Args:
        team_id: Team ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of team members

    Raises:
        HTTPException: If team not found or user is not a member
    """
    team_manager = TeamManager(db)
    team = team_manager.get_team_by_id(team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )

    # Check if user is a member
    check_team_member(team_id, current_user.id, db)

    members = team_manager.get_team_members(team_id)
    return [TeamMemberResponse.model_validate(member) for member in members]
