"""Test database models"""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import (
    Base,
    User,
    Team,
    TeamMember,
    TeamRole,
    File,
    ShareLink,
    Workspace,
)


@pytest.fixture(scope="function")
def db_session():
    """Create a test database session"""
    # Use in-memory SQLite for testing
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def test_workspace(db_session: Session):
    owner = User(
        username="workspace-owner",
        email="workspace-owner@example.com",
        password_hash="hash",
        full_name="Workspace Owner",
    )
    db_session.add(owner)
    db_session.commit()

    workspace = Workspace(
        name="Test Workspace",
        slug="test-workspace",
        owner_id=owner.id,
    )
    db_session.add(workspace)
    db_session.commit()
    db_session.refresh(workspace)
    return workspace


class TestUserModel:
    """Test User model"""

    def test_create_user(self, db_session: Session):
        """Test creating a user"""
        user = User(
            username="testuser",
            email="test@example.com",
            password_hash="hashed_password",
            full_name="Test User",
            is_active=True
        )
        db_session.add(user)
        db_session.commit()

        assert user.id is not None
        assert user.username == "testuser"
        assert user.email == "test@example.com"
        assert user.is_active is True
        assert user.created_at is not None
        assert user.updated_at is not None

    def test_user_unique_username(self, db_session: Session):
        """Test username uniqueness constraint"""
        user1 = User(
            username="testuser",
            email="test1@example.com",
            password_hash="hash1",
            full_name="User 1"
        )
        user2 = User(
            username="testuser",
            email="test2@example.com",
            password_hash="hash2",
            full_name="User 2"
        )

        db_session.add(user1)
        db_session.commit()

        db_session.add(user2)
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_user_unique_email(self, db_session: Session):
        """Test email uniqueness constraint"""
        user1 = User(
            username="user1",
            email="test@example.com",
            password_hash="hash1",
            full_name="User 1"
        )
        user2 = User(
            username="user2",
            email="test@example.com",
            password_hash="hash2",
            full_name="User 2"
        )

        db_session.add(user1)
        db_session.commit()

        db_session.add(user2)
        with pytest.raises(IntegrityError):
            db_session.commit()


class TestTeamModel:
    """Test Team and TeamMember models"""

    def test_create_team(self, db_session: Session):
        """Test creating a team"""
        user = User(
            username="owner",
            email="owner@example.com",
            password_hash="hash",
            full_name="Owner"
        )
        db_session.add(user)
        db_session.commit()

        team = Team(
            name="Test Team",
            description="A test team",
            owner_id=user.id
        )
        db_session.add(team)
        db_session.commit()

        assert team.id is not None
        assert team.name == "Test Team"
        assert team.owner_id == user.id
        assert team.owner.username == "owner"

    def test_team_members(self, db_session: Session):
        """Test team membership"""
        owner = User(username="owner", email="owner@example.com", password_hash="hash", full_name="Owner")
        member = User(username="member", email="member@example.com", password_hash="hash", full_name="Member")
        db_session.add_all([owner, member])
        db_session.commit()

        team = Team(name="Test Team", owner_id=owner.id)
        db_session.add(team)
        db_session.commit()

        team_member = TeamMember(
            team_id=team.id,
            user_id=member.id,
            role=TeamRole.MEMBER
        )
        db_session.add(team_member)
        db_session.commit()

        assert len(team.members) == 1
        assert team.members[0].user.username == "member"
        assert team.members[0].role == TeamRole.MEMBER

    def test_team_member_unique_constraint(self, db_session: Session):
        """Test team member uniqueness constraint"""
        user = User(username="user", email="user@example.com", password_hash="hash", full_name="User")
        db_session.add(user)
        db_session.commit()

        team = Team(name="Team", owner_id=user.id)
        db_session.add(team)
        db_session.commit()

        member1 = TeamMember(team_id=team.id, user_id=user.id, role=TeamRole.MEMBER)
        member2 = TeamMember(team_id=team.id, user_id=user.id, role=TeamRole.ADMIN)

        db_session.add(member1)
        db_session.commit()

        db_session.add(member2)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()


class TestFileModel:
    """Test File model"""

    def test_create_file(self, db_session: Session, test_workspace: Workspace):
        """Test creating a file"""
        user = User(username="user", email="user@example.com", password_hash="hash", full_name="User")
        db_session.add(user)
        db_session.commit()

        file = File(
            uri="/test/file.txt",
            name="file.txt",
            owner_id=user.id,
            workspace_id=test_workspace.id,
            is_directory=False,
            size=1024,
            mime_type="text/plain"
        )
        db_session.add(file)
        db_session.commit()

        assert file.id is not None
        assert file.uri == "/test/file.txt"
        assert file.name == "file.txt"
        assert file.owner.username == "user"
        assert file.is_directory is False
        assert file.size == 1024

    def test_file_hierarchy(self, db_session: Session, test_workspace: Workspace):
        """Test file parent-child relationship"""
        user = User(username="user", email="user@example.com", password_hash="hash", full_name="User")
        db_session.add(user)
        db_session.commit()

        parent = File(
            uri="/test",
            name="test",
            owner_id=user.id,
            workspace_id=test_workspace.id,
            is_directory=True
        )
        db_session.add(parent)
        db_session.commit()

        child = File(
            uri="/test/file.txt",
            name="file.txt",
            owner_id=user.id,
            workspace_id=test_workspace.id,
            parent_id=parent.id,
            is_directory=False
        )
        db_session.add(child)
        db_session.commit()

        assert len(parent.children) == 1
        assert parent.children[0].name == "file.txt"
        assert child.parent.name == "test"


class TestShareLinkModel:
    """Test ShareLink model"""

    def test_create_share_link(self, db_session: Session, test_workspace: Workspace):
        """Test creating a share link"""
        user = User(username="user", email="user@example.com", password_hash="hash", full_name="User")
        db_session.add(user)
        db_session.commit()

        file = File(uri="/file.txt", name="file.txt", owner_id=user.id, workspace_id=test_workspace.id)
        db_session.add(file)
        db_session.commit()

        share_link = ShareLink(
            file_id=file.id,
            token="abc123xyz",
            created_by=user.id,
            access_count=0
        )
        db_session.add(share_link)
        db_session.commit()

        assert share_link.id is not None
        assert share_link.token == "abc123xyz"
        assert share_link.access_count == 0
        assert share_link.creator.username == "user"

    def test_share_link_with_expiration(self, db_session: Session, test_workspace: Workspace):
        """Test share link with expiration"""
        user = User(username="user", email="user@example.com", password_hash="hash", full_name="User")
        db_session.add(user)
        db_session.commit()

        file = File(uri="/file.txt", name="file.txt", owner_id=user.id, workspace_id=test_workspace.id)
        db_session.add(file)
        db_session.commit()

        expires_at = datetime.utcnow() + timedelta(days=7)
        share_link = ShareLink(
            file_id=file.id,
            token="token123",
            created_by=user.id,
            expires_at=expires_at,
            max_access_count=10
        )
        db_session.add(share_link)
        db_session.commit()

        assert share_link.expires_at is not None
        assert share_link.max_access_count == 10

    def test_share_link_unique_token(self, db_session: Session, test_workspace: Workspace):
        """Test share link token uniqueness"""
        user = User(username="user", email="user@example.com", password_hash="hash", full_name="User")
        db_session.add(user)
        db_session.commit()

        file1 = File(uri="/file1.txt", name="file1.txt", owner_id=user.id, workspace_id=test_workspace.id)
        file2 = File(uri="/file2.txt", name="file2.txt", owner_id=user.id, workspace_id=test_workspace.id)
        db_session.add_all([file1, file2])
        db_session.commit()

        link1 = ShareLink(file_id=file1.id, token="sametoken", created_by=user.id)
        link2 = ShareLink(file_id=file2.id, token="sametoken", created_by=user.id)

        db_session.add(link1)
        db_session.commit()

        db_session.add(link2)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()


class TestRelationships:
    """Test model relationships"""

    def test_cascade_delete_user_files(self, db_session: Session, test_workspace: Workspace):
        """Test cascade delete when user is deleted"""
        user = User(username="user", email="user@example.com", password_hash="hash", full_name="User")
        db_session.add(user)
        db_session.commit()

        file = File(uri="/file.txt", name="file.txt", owner_id=user.id, workspace_id=test_workspace.id)
        db_session.add(file)
        db_session.commit()

        file_id = file.id

        db_session.delete(user)
        db_session.commit()

        # File should be deleted due to cascade
        deleted_file = db_session.get(File, file_id)
        assert deleted_file is None
