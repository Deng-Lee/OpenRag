"""User management service - handles user CRUD operations and authentication"""

from typing import Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openrag.models.user import User
from openrag.security import hash_password, verify_password


class UserManager:
    """User management service for CRUD operations and authentication"""

    def __init__(self, session: Session):
        """
        Initialize UserManager with database session

        Args:
            session: SQLAlchemy database session
        """
        self.session = session

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        full_name: str,
        is_admin: bool = False
    ) -> User:
        """
        Create a new user

        Args:
            username: Unique username
            email: Unique email address
            password: Plain text password (will be hashed)
            full_name: User's full name

        Returns:
            Created User object

        Raises:
            ValueError: If username/email already exists or invalid input
        """
        if not username or not username.strip():
            raise ValueError("Username cannot be empty")
        if not email or not email.strip():
            raise ValueError("Email cannot be empty")
        if not password:
            raise ValueError("Password cannot be empty")
        if not full_name or not full_name.strip():
            raise ValueError("Full name cannot be empty")

        # Check for existing username or email
        existing = self.session.query(User).filter(
            or_(User.username == username, User.email == email)
        ).first()

        if existing:
            if existing.username == username:
                raise ValueError(f"Username '{username}' already exists")
            if existing.email == email:
                raise ValueError(f"Email '{email}' already exists")

        # Hash password
        password_hash = hash_password(password)

        # Create user
        user = User(
            username=username,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            is_active=True,
            is_admin=is_admin
        )

        try:
            self.session.add(user)
            self.session.commit()
            self.session.refresh(user)
            return user
        except IntegrityError as e:
            self.session.rollback()
            raise ValueError(f"Database integrity error: {str(e)}")

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        """
        Get user by ID

        Args:
            user_id: User ID

        Returns:
            User object or None if not found
        """
        return self.session.query(User).filter(User.id == user_id).first()

    def get_user_by_username(self, username: str) -> Optional[User]:
        """
        Get user by username

        Args:
            username: Username

        Returns:
            User object or None if not found
        """
        return self.session.query(User).filter(User.username == username).first()

    def get_user_by_email(self, email: str) -> Optional[User]:
        """
        Get user by email

        Args:
            email: Email address

        Returns:
            User object or None if not found
        """
        return self.session.query(User).filter(User.email == email).first()

    def update_user(self, user_id: int, **kwargs) -> Optional[User]:
        """
        Update user with partial or full data

        Args:
            user_id: User ID
            **kwargs: Fields to update (username, email, password, full_name, is_active)

        Returns:
            Updated User object or None if not found

        Raises:
            ValueError: If username/email already exists or invalid input
        """
        user = self.get_user_by_id(user_id)
        if not user:
            return None

        # Validate and check for duplicates
        if 'username' in kwargs:
            new_username = kwargs['username']
            if not new_username or not new_username.strip():
                raise ValueError("Username cannot be empty")
            if new_username != user.username:
                existing = self.get_user_by_username(new_username)
                if existing:
                    raise ValueError(f"Username '{new_username}' already exists")
                user.username = new_username

        if 'email' in kwargs:
            new_email = kwargs['email']
            if not new_email or not new_email.strip():
                raise ValueError("Email cannot be empty")
            if new_email != user.email:
                existing = self.get_user_by_email(new_email)
                if existing:
                    raise ValueError(f"Email '{new_email}' already exists")
                user.email = new_email

        if 'password' in kwargs:
            new_password = kwargs['password']
            if not new_password:
                raise ValueError("Password cannot be empty")
            user.password_hash = hash_password(new_password)

        if 'full_name' in kwargs:
            new_full_name = kwargs['full_name']
            if not new_full_name or not new_full_name.strip():
                raise ValueError("Full name cannot be empty")
            user.full_name = new_full_name

        if 'is_active' in kwargs:
            user.is_active = bool(kwargs['is_active'])

        if 'is_admin' in kwargs:
            user.is_admin = bool(kwargs['is_admin'])

        try:
            self.session.commit()
            self.session.refresh(user)
            return user
        except IntegrityError as e:
            self.session.rollback()
            raise ValueError(f"Database integrity error: {str(e)}")

    def delete_user(self, user_id: int) -> bool:
        """
        Delete user (hard delete)

        Args:
            user_id: User ID

        Returns:
            True if deleted, False if user not found
        """
        user = self.get_user_by_id(user_id)
        if not user:
            return False

        try:
            self.session.delete(user)
            self.session.commit()
            return True
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete user: {str(e)}")

    def authenticate_user(self, username: str, password: str) -> Optional[User]:
        """
        Authenticate user with username and password

        Args:
            username: Username
            password: Plain text password

        Returns:
            User object if authentication successful, None otherwise
        """
        if not username or not password:
            return None

        user = self.get_user_by_username(username)
        if not user:
            return None

        if not user.is_active:
            return None

        if not verify_password(password, user.password_hash):
            return None

        return user

    def list_users(self, skip: int = 0, limit: int = 100) -> list[User]:
        """
        List users with pagination

        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of User objects
        """
        return self.session.query(User).offset(skip).limit(limit).all()
