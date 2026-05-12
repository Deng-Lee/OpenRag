"""Tests for permission management service"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models.base import Base
from openrag.models.file import File
from openrag.models.permission import EntityType, FilePermission, Permission
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.user import User
from openrag.services.permission_manager import PermissionManager


@pytest.fixture
def engine():
    """Create in-memory SQLite database engine"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session"""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def permission_manager(session):
    """Create PermissionManager instance"""
    return PermissionManager(session)


@pytest.fixture
def sample_user(session):
    """Create a sample user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash="hashed_password",
        full_name="Test User",
        is_active=True
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture
def sample_user2(session):
    """Create a second sample user"""
    user = User(
        username="testuser2",
        email="test2@example.com",
        password_hash="hashed_password",
        full_name="Test User 2",
        is_active=True
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture
def sample_team(session, sample_user):
    """Create a sample team"""
    team = Team(
        name="Test Team",
        description="A test team",
        owner_id=sample_user.id
    )
    session.add(team)
    session.commit()
    session.refresh(team)
    return team


@pytest.fixture
def sample_file(session, sample_user):
    """Create a sample file"""
    file = File(
        uri="/test/file.txt",
        name="file.txt",
        owner_id=sample_user.id,
        is_directory=False,
        size=1024,
        mime_type="text/plain"
    )
    session.add(file)
    session.commit()
    session.refresh(file)
    return file


class TestGrantPermission:
    """Test grant_permission method"""

    def test_grant_permission_to_user(self, permission_manager, sample_user, sample_user2, sample_file):
        """Test granting read permission to user"""
        perm = permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )

        assert perm.id is not None
        assert perm.file_id == sample_file.id
        assert perm.entity_type == EntityType.USER
        assert perm.entity_id == sample_user2.id
        assert perm.permission == Permission.READ

    def test_grant_permission_to_team(self, permission_manager, sample_user, sample_team, sample_file):
        """Test granting write permission to team"""
        perm = permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.TEAM,
            entity_id=sample_team.id,
            permission="write"
        )

        assert perm.id is not None
        assert perm.file_id == sample_file.id
        assert perm.entity_type == EntityType.TEAM
        assert perm.entity_id == sample_team.id
        assert perm.permission == Permission.WRITE

    def test_grant_permission_duplicate(self, permission_manager, sample_user, sample_user2, sample_file):
        """Test preventing duplicate permissions"""
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )

        with pytest.raises(ValueError, match="Permission already exists"):
            permission_manager.grant_permission(
                file_id=sample_file.id,
                entity_type=EntityType.USER,
                entity_id=sample_user2.id,
                permission="write"
            )

    def test_grant_permission_invalid_file(self, permission_manager, sample_user2):
        """Test raising error for non-existent file"""
        with pytest.raises(ValueError, match="File with id 99999 not found"):
            permission_manager.grant_permission(
                file_id=99999,
                entity_type=EntityType.USER,
                entity_id=sample_user2.id,
                permission="read"
            )

    def test_grant_permission_invalid_user(self, permission_manager, sample_file):
        """Test raising error for non-existent user"""
        with pytest.raises(ValueError, match="User with id 99999 not found"):
            permission_manager.grant_permission(
                file_id=sample_file.id,
                entity_type=EntityType.USER,
                entity_id=99999,
                permission="read"
            )

    def test_grant_permission_invalid_team(self, permission_manager, sample_file):
        """Test raising error for non-existent team"""
        with pytest.raises(ValueError, match="Team with id 99999 not found"):
            permission_manager.grant_permission(
                file_id=sample_file.id,
                entity_type=EntityType.TEAM,
                entity_id=99999,
                permission="read"
            )

    def test_grant_permission_invalid_permission(self, permission_manager, sample_user2, sample_file):
        """Test raising error for invalid permission"""
        with pytest.raises(ValueError, match="Invalid permission"):
            permission_manager.grant_permission(
                file_id=sample_file.id,
                entity_type=EntityType.USER,
                entity_id=sample_user2.id,
                permission="invalid"
            )


class TestRevokePermission:
    """Test revoke_permission method"""

    def test_revoke_permission(self, permission_manager, sample_user, sample_user2, sample_file):
        """Test revoking permission successfully"""
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )

        result = permission_manager.revoke_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id
        )

        assert result is True

    def test_revoke_permission_not_found(self, permission_manager, sample_user2, sample_file):
        """Test returning False when permission doesn't exist"""
        result = permission_manager.revoke_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id
        )

        assert result is False


class TestCheckPermission:
    """Test check_permission method"""

    def test_check_permission_owner(self, permission_manager, sample_user, sample_file):
        """Test owner has all permissions"""
        assert permission_manager.check_permission(sample_user.id, sample_file.id, "read") is True
        assert permission_manager.check_permission(sample_user.id, sample_file.id, "write") is True
        assert permission_manager.check_permission(sample_user.id, sample_file.id, "admin") is True

    def test_check_permission_direct_user(self, permission_manager, sample_user, sample_user2, sample_file):
        """Test direct user permission works"""
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )

        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "read") is True
        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "write") is False

    def test_check_permission_team(self, permission_manager, session, sample_user, sample_user2, sample_team, sample_file):
        """Test team permission works"""
        # Add user2 to team
        member = TeamMember(
            team_id=sample_team.id,
            user_id=sample_user2.id,
            role=TeamRole.MEMBER
        )
        session.add(member)
        session.commit()

        # Grant permission to team
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.TEAM,
            entity_id=sample_team.id,
            permission="write"
        )

        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "read") is True
        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "write") is True
        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "admin") is False

    def test_check_permission_hierarchy(self, permission_manager, sample_user, sample_user2, sample_file):
        """Test admin implies write and read"""
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="admin"
        )

        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "read") is True
        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "write") is True
        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "admin") is True

    def test_check_permission_inheritance(self, permission_manager, session, sample_user, sample_user2):
        """Test parent directory permission inherited"""
        # Create parent directory
        parent_dir = File(
            uri="/test",
            name="test",
            owner_id=sample_user.id,
            is_directory=True,
            size=0
        )
        session.add(parent_dir)
        session.commit()
        session.refresh(parent_dir)

        # Create child file
        child_file = File(
            uri="/test/child.txt",
            name="child.txt",
            owner_id=sample_user.id,
            parent_id=parent_dir.id,
            is_directory=False,
            size=1024,
            mime_type="text/plain"
        )
        session.add(child_file)
        session.commit()
        session.refresh(child_file)

        # Grant permission on parent
        permission_manager.grant_permission(
            file_id=parent_dir.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )

        # Check child inherits permission
        assert permission_manager.check_permission(sample_user2.id, child_file.id, "read") is True

    def test_check_permission_denied(self, permission_manager, sample_user, sample_user2, sample_file):
        """Test no permission returns False"""
        assert permission_manager.check_permission(sample_user2.id, sample_file.id, "read") is False


class TestGetFilePermissions:
    """Test get_file_permissions method"""

    def test_get_file_permissions(self, permission_manager, sample_user, sample_user2, sample_team, sample_file):
        """Test getting all permissions for a file"""
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.TEAM,
            entity_id=sample_team.id,
            permission="write"
        )

        perms = permission_manager.get_file_permissions(sample_file.id)

        assert len(perms) == 2
        assert any(p.entity_type == EntityType.USER and p.entity_id == sample_user2.id for p in perms)
        assert any(p.entity_type == EntityType.TEAM and p.entity_id == sample_team.id for p in perms)


class TestGetUserPermissions:
    """Test get_user_permissions method"""

    def test_get_user_permissions(self, permission_manager, session, sample_user, sample_user2, sample_team, sample_file):
        """Test getting all permissions user has for a file"""
        # Add user2 to team
        member = TeamMember(
            team_id=sample_team.id,
            user_id=sample_user2.id,
            role=TeamRole.MEMBER
        )
        session.add(member)
        session.commit()

        # Grant direct user permission
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )

        # Grant team permission
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.TEAM,
            entity_id=sample_team.id,
            permission="write"
        )

        perms = permission_manager.get_user_permissions(sample_user2.id, sample_file.id)

        assert "read" in perms
        assert "write" in perms


class TestListAccessibleFiles:
    """Test list_accessible_files method"""

    def test_list_accessible_files(self, permission_manager, session, sample_user, sample_user2, sample_team, sample_file):
        """Test listing all files user can access"""
        # Create another file owned by user1
        file2 = File(
            uri="/test/file2.txt",
            name="file2.txt",
            owner_id=sample_user.id,
            is_directory=False,
            size=2048,
            mime_type="text/plain"
        )
        session.add(file2)
        session.commit()
        session.refresh(file2)

        # Create file owned by user2
        file3 = File(
            uri="/test/file3.txt",
            name="file3.txt",
            owner_id=sample_user2.id,
            is_directory=False,
            size=512,
            mime_type="text/plain"
        )
        session.add(file3)
        session.commit()
        session.refresh(file3)

        # Grant permission to user2 for file1
        permission_manager.grant_permission(
            file_id=sample_file.id,
            entity_type=EntityType.USER,
            entity_id=sample_user2.id,
            permission="read"
        )

        # Add user2 to team and grant team permission for file2
        member = TeamMember(
            team_id=sample_team.id,
            user_id=sample_user2.id,
            role=TeamRole.MEMBER
        )
        session.add(member)
        session.commit()

        permission_manager.grant_permission(
            file_id=file2.id,
            entity_type=EntityType.TEAM,
            entity_id=sample_team.id,
            permission="write"
        )

        files = permission_manager.list_accessible_files(sample_user2.id)

        # User2 should have access to: file1 (direct), file2 (team), file3 (owned)
        assert len(files) == 3
        file_ids = [f.id for f in files]
        assert sample_file.id in file_ids
        assert file2.id in file_ids
        assert file3.id in file_ids
