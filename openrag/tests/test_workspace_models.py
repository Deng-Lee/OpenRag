"""Test workspace models"""

import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, User
from openrag.models.workspace import Workspace, WorkspaceMember


@pytest.fixture(scope="function")
def db_session():
    """Create a test database session"""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def test_user(db_session: Session):
    """Create a test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash="hashed_password",
        full_name="Test User",
        is_active=True
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_user2(db_session: Session):
    """Create a second test user"""
    user = User(
        username="testuser2",
        email="test2@example.com",
        password_hash="hashed_password",
        full_name="Test User 2",
        is_active=True
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestWorkspaceModel:
    """Test Workspace model"""

    def test_create_workspace(self, db_session: Session, test_user: User):
        """Test creating a workspace with all fields"""
        workspace = Workspace(
            name="Test Workspace",
            slug="test-workspace",
            description="A test workspace",
            owner_id=test_user.id,
            max_concurrent_tasks=5,
            max_storage_bytes=5368709120,  # 5GB
            priority_strategy="file_size"
        )
        db_session.add(workspace)
        db_session.commit()
        db_session.refresh(workspace)

        assert workspace.id is not None
        assert workspace.name == "Test Workspace"
        assert workspace.slug == "test-workspace"
        assert workspace.description == "A test workspace"
        assert workspace.owner_id == test_user.id
        assert workspace.max_concurrent_tasks == 5
        assert workspace.max_storage_bytes == 5368709120
        assert workspace.priority_strategy == "file_size"
        assert isinstance(workspace.created_at, datetime)
        assert isinstance(workspace.updated_at, datetime)
        assert workspace.owner == test_user

    def test_workspace_slug_unique(self, db_session: Session, test_user: User):
        """Test slug uniqueness constraint"""
        workspace1 = Workspace(
            name="Workspace 1",
            slug="unique-slug",
            owner_id=test_user.id
        )
        db_session.add(workspace1)
        db_session.commit()

        workspace2 = Workspace(
            name="Workspace 2",
            slug="unique-slug",
            owner_id=test_user.id
        )
        db_session.add(workspace2)

        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_workspace_defaults(self, db_session: Session, test_user: User):
        """Test workspace default values"""
        workspace = Workspace(
            name="Default Workspace",
            slug="default-workspace",
            owner_id=test_user.id
        )
        db_session.add(workspace)
        db_session.commit()
        db_session.refresh(workspace)

        assert workspace.max_concurrent_tasks == 10
        assert workspace.max_storage_bytes == 10737418240  # 10GB
        assert workspace.priority_strategy == "file_size"
        assert workspace.description is None


class TestWorkspaceMemberModel:
    """Test WorkspaceMember model"""

    def test_create_workspace_member(self, db_session: Session, test_user: User, test_user2: User):
        """Test creating a workspace member"""
        workspace = Workspace(
            name="Test Workspace",
            slug="test-workspace",
            owner_id=test_user.id
        )
        db_session.add(workspace)
        db_session.commit()
        db_session.refresh(workspace)

        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=test_user2.id,
            role="member"
        )
        db_session.add(member)
        db_session.commit()
        db_session.refresh(member)

        assert member.id is not None
        assert member.workspace_id == workspace.id
        assert member.user_id == test_user2.id
        assert member.role == "member"
        assert isinstance(member.joined_at, datetime)
        assert member.workspace == workspace
        assert member.user == test_user2

    def test_workspace_member_unique_constraint(self, db_session: Session, test_user: User, test_user2: User):
        """Test user can only be added once to a workspace"""
        workspace = Workspace(
            name="Test Workspace",
            slug="test-workspace",
            owner_id=test_user.id
        )
        db_session.add(workspace)
        db_session.commit()
        db_session.refresh(workspace)

        member1 = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=test_user2.id,
            role="member"
        )
        db_session.add(member1)
        db_session.commit()

        member2 = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=test_user2.id,
            role="admin"
        )
        db_session.add(member2)

        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_workspace_member_cascade_delete(self, db_session: Session, test_user: User, test_user2: User):
        """Test members deleted when workspace deleted"""
        workspace = Workspace(
            name="Test Workspace",
            slug="test-workspace",
            owner_id=test_user.id
        )
        db_session.add(workspace)
        db_session.commit()
        db_session.refresh(workspace)

        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=test_user2.id,
            role="member"
        )
        db_session.add(member)
        db_session.commit()

        member_id = member.id

        # Delete workspace
        db_session.delete(workspace)
        db_session.commit()

        # Verify member was cascade deleted
        deleted_member = db_session.get(WorkspaceMember, member_id)
        assert deleted_member is None

    def test_workspace_member_default_role(self, db_session: Session, test_user: User, test_user2: User):
        """Test workspace member default role"""
        workspace = Workspace(
            name="Test Workspace",
            slug="test-workspace",
            owner_id=test_user.id
        )
        db_session.add(workspace)
        db_session.commit()
        db_session.refresh(workspace)

        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=test_user2.id
        )
        db_session.add(member)
        db_session.commit()
        db_session.refresh(member)

        assert member.role == "member"
