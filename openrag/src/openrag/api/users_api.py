"""User Management API - Registration, Login, and Profile Management"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Form
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import select

from openrag.api.deps import get_db, get_current_active_user
from openrag.models.user import User
from openrag.security import create_access_token, verify_password
from openrag.services.user_manager import UserManager


router = APIRouter(prefix="/users", tags=["users"])


# Pydantic Models
class UserRegisterRequest(BaseModel):
    """User registration request"""

    username: str = Field(..., min_length=1, max_length=64)
    email: EmailStr
    password: str = Field(..., min_length=1)
    full_name: str = Field(..., min_length=1, max_length=128)


class UserLoginRequest(BaseModel):
    """User login request"""

    email: EmailStr
    password: str


class UserUpdateRequest(BaseModel):
    """User profile update request"""

    username: Optional[str] = Field(None, min_length=1, max_length=64)
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, min_length=1, max_length=128)


class UserResponse(BaseModel):
    """User response model"""

    id: int
    username: str
    email: str
    full_name: str
    is_active: bool
    is_admin: bool

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    """Login response with token and user info"""

    access_token: str
    token_type: str
    user: UserResponse


# API Endpoints
@router.post(
    "/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
async def register(request: UserRegisterRequest, db: Session = Depends(get_db)):
    """
    Register a new user.

    Args:
        request: User registration data
        db: Database session

    Returns:
        Created user information

    Raises:
        HTTPException: If username or email already exists
    """
    user_manager = UserManager(db)

    try:
        user = user_manager.create_user(
            username=request.username,
            email=request.email,
            password=request.password,
            full_name=request.full_name,
        )
        return UserResponse.model_validate(user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/login", response_model=LoginResponse)
async def login(
    email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    """
    Login and get JWT access token.

    Args:
        email: User email (form field)
        password: User password (form field)
        db: Database session

    Returns:
        Access token and user information

    Raises:
        HTTPException: If credentials are invalid or user is inactive
    """
    user_manager = UserManager(db)

    # Find user by email
    user = user_manager.get_user_by_email(email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user account"
        )

    # Verify password
    if not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    # Create access token
    access_token = create_access_token(data={"sub": str(user.id)})

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: User = Depends(get_current_active_user),
):
    """
    Get current user profile.

    Args:
        current_user: Current authenticated user

    Returns:
        Current user information
    """
    return UserResponse.model_validate(current_user)


@router.put("/me", response_model=UserResponse)
async def update_current_user_profile(
    request: UserUpdateRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Update current user profile.

    Args:
        request: Update data
        current_user: Current authenticated user
        db: Database session

    Returns:
        Updated user information

    Raises:
        HTTPException: If username or email already exists
    """
    user_manager = UserManager(db)

    # Build update dict with only provided fields
    update_data = {}
    if request.username is not None:
        update_data["username"] = request.username
    if request.email is not None:
        update_data["email"] = request.email
    if request.full_name is not None:
        update_data["full_name"] = request.full_name

    try:
        updated_user = user_manager.update_user(current_user.id, **update_data)
        if not updated_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        return UserResponse.model_validate(updated_user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/{user_id}", response_model=UserResponse)
async def get_user_by_id(
    user_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Get user by ID.

    Args:
        user_id: User ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        User information

    Raises:
        HTTPException: If user not found
    """
    user_manager = UserManager(db)
    user = user_manager.get_user_by_id(user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    return UserResponse.model_validate(user)


from sqlalchemy import select
from openrag.models.role import Role, UserRole


class UserRoleCreate(BaseModel):
    role_id: int


@router.get("/", response_model=list[UserResponse])
def get_all_users(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    users = db.scalars(select(User)).all()
    return [UserResponse.model_validate(u) for u in users]


@router.post("/{user_id}/roles")
def assign_role_to_user(
    user_id: int,
    data: UserRoleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    user = db.scalar(select(User).where(User.id == user_id))
    role = db.scalar(select(Role).where(Role.id == data.role_id))
    if not user or not role:
        raise HTTPException(status_code=404, detail="User or Role not found")

    existing = db.scalar(
        select(UserRole).where(
            UserRole.user_id == user_id, UserRole.role_id == data.role_id
        )
    )
    if existing:
        return {"message": "Role already assigned"}

    user_role = UserRole(user_id=user_id, role_id=data.role_id)
    db.add(user_role)
    db.commit()
    return {"message": "Role assigned successfully"}


@router.delete("/{user_id}/roles/{role_id}")
def remove_role_from_user(
    user_id: int,
    role_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    user_role = db.scalar(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
    )
    if user_role:
        db.delete(user_role)
        db.commit()
    return {"message": "Role removed successfully"}
