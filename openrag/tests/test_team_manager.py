"""Tests for team management service"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models.base import Base
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.user import User
from openrag.services.team_manager import TeamManager
from openrag.services.user_manager import UserManager


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
def user_manager(session):
    """Create UserManager instance"""
    return UserManager(session)


@pytest.fixture
def team_manager(session):
    """Create TeamManager instance"""
    return TeamManager(session)


@pytest.fixture
def sample_user(user_manager):
    """Create a sample user for testing"""
    return user_manager.create_user(
        username="testuser",
        email="test@example.com",
        password="password123",
        full_name="Test User"
    )


@pytest.fixture
def sample_user2(user_manager):
    """Create a second sample user for testing"""
    return user_manager.create_user(
        username="testuser2",
        email="test2@example.com",
        password="password123",
        full_name="Test User 2"
    )


class TestTeamCreation:
    """Test team creation"""

    def test_create_team_success(self, team_manager, sample_user):
        """Test successful team creation"""
        team = team_manager.create_team(
            name="Test Team",
            description="A test team",
            owner_id=sample_user.id
        )

        assert team.id is not None
        assert team.name == "Test Team"
        assert team.description == "A test team"
        assert team.owner_id == sample_user.id

    def test_create_team_duplicate_name(self, team_manager, sample_user):
        """Test creating team with duplicate name"""
        team_manager.create_team(
            name="Test Team",
            description="First team",
            owner_id=sample_user.id
        )

        with pytest.raises(ValueError, match="Team name 'Test Team' already exists"):
            team_manager.create_team(
                name="Test Team",
                description="Second team",
                owner_id=sample_user.id
            )

    def test_create_team_empty_name(self, team_manager, sample_user):
        """Test creating team with empty name"""
        with pytest.raises(ValueError, match="Team name cannot be empty"):
            team_manager.create_team(
                name="",
                description="A test team",
                owner_id=sample_user.id
            )

    def test_create_team_nonexistent_owner(self, team_manager):
        """Test creating team with non-existent owner"""
        with pytest.raises(ValueError, match="Owner user with ID 99999 does not exist"):
            team_manager.create_team(
                name="Test Team",
                description="A test team",
                owner_id=99999
            )


class TestTeamRetrieval:
    """Test team retrieval methods"""

    @pytest.fixture
    def sample_team(self, team_manager, sample_user):
        """Create a sample team for testing"""
        return team_manager.create_team(
            name="Test Team",
            description="A test team",
            owner_id=sample_user.id
        )

    def test_get_team_by_id_success(self, team_manager, sample_team):
        """Test getting team by ID"""
        team = team_manager.get_team_by_id(sample_team.id)
        assert team is not None
        assert team.id == sample_team.id
        assert team.name == "Test Team"

    def test_get_team_by_id_not_found(self, team_manager):
        """Test getting non-existent team by ID"""
        team = team_manager.get_team_by_id(99999)
        assert team is None

    def test_get_team_by_name_success(self, team_manager, sample_team):
        """Test getting team by name"""
        team = team_manager.get_team_by_name("Test Team")
        assert team is not None
        assert team.id == sample_team.id
        assert team.name == "Test Team"

    def test_get_team_by_name_not_found(self, team_manager):
        """Test getting non-existent team by name"""
        team = team_manager.get_team_by_name("Nonexistent Team")
        assert team is None


class TestTeamUpdate:
    """Test team update operations"""

    @pytest.fixture
    def sample_team(self, team_manager, sample_user):
        """Create a sample team for testing"""
        return team_manager.create_team(
            name="Test Team",
            description="A test team",
            owner_id=sample_user.id
        )

    def test_update_team_name(self, team_manager, sample_team):
        """Test updating team name"""
        updated = team_manager.update_team(sample_team.id, name="New Team Name")
        assert updated is not None
        assert updated.name == "New Team Name"
        assert updated.description == "A test team"

    def test_update_team_description(self, team_manager, sample_team):
        """Test updating team description"""
        updated = team_manager.update_team(sample_team.id, description="New description")
        assert updated is not None
        assert updated.description == "New description"
        assert updated.name == "Test Team"

    def test_update_team_multiple_fields(self, team_manager, sample_team):
        """Test updating multiple fields at once"""
        updated = team_manager.update_team(
            sample_team.id,
            name="New Team Name",
            description="New description"
        )
        assert updated is not None
        assert updated.name == "New Team Name"
        assert updated.description == "New description"

    def test_update_team_not_found(self, team_manager):
        """Test updating non-existent team"""
        result = team_manager.update_team(99999, name="New Name")
        assert result is None

    def test_update_team_duplicate_name(self, team_manager, sample_user, sample_team):
        """Test updating to duplicate team name"""
        team_manager.create_team(
            name="Other Team",
            description="Another team",
            owner_id=sample_user.id
        )

        with pytest.raises(ValueError, match="Team name 'Other Team' already exists"):
            team_manager.update_team(sample_team.id, name="Other Team")

    def test_update_team_empty_name(self, team_manager, sample_team):
        """Test updating to empty team name"""
        with pytest.raises(ValueError, match="Team name cannot be empty"):
            team_manager.update_team(sample_team.id, name="")


class TestTeamDeletion:
    """Test team deletion"""

    @pytest.fixture
    def sample_team(self, team_manager, sample_user):
        """Create a sample team for testing"""
        return team_manager.create_team(
            name="Test Team",
            description="A test team",
            owner_id=sample_user.id
        )

    def test_delete_team_success(self, team_manager, sample_team):
        """Test successful team deletion"""
        result = team_manager.delete_team(sample_team.id)
        assert result is True

        # Verify team is deleted
        team = team_manager.get_team_by_id(sample_team.id)
        assert team is None

    def test_delete_team_not_found(self, team_manager):
        """Test deleting non-existent team"""
        result = team_manager.delete_team(99999)
        assert result is False

    def test_delete_team_cascades_members(self, team_manager, sample_user, sample_user2, sample_team):
        """Test that deleting team cascades to team members"""
        # Add a member to the team
        team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.MEMBER)

        # Verify member exists
        members = team_manager.get_team_members(sample_team.id)
        assert len(members) == 1

        # Delete team
        result = team_manager.delete_team(sample_team.id)
        assert result is True

        # Verify team and members are deleted
        team = team_manager.get_team_by_id(sample_team.id)
        assert team is None


class TestTeamListing:
    """Test team listing with pagination"""

    @pytest.fixture
    def multiple_teams(self, team_manager, sample_user):
        """Create multiple teams for testing"""
        teams = []
        for i in range(15):
            team = team_manager.create_team(
                name=f"Team {i}",
                description=f"Team {i} description",
                owner_id=sample_user.id
            )
            teams.append(team)
        return teams

    def test_list_teams_default(self, team_manager, multiple_teams):
        """Test listing teams with default pagination"""
        teams = team_manager.list_teams()
        assert len(teams) == 15

    def test_list_teams_with_limit(self, team_manager, multiple_teams):
        """Test listing teams with limit"""
        teams = team_manager.list_teams(limit=5)
        assert len(teams) == 5

    def test_list_teams_with_skip(self, team_manager, multiple_teams):
        """Test listing teams with skip"""
        teams = team_manager.list_teams(skip=10)
        assert len(teams) == 5

    def test_list_teams_with_skip_and_limit(self, team_manager, multiple_teams):
        """Test listing teams with skip and limit"""
        teams = team_manager.list_teams(skip=5, limit=5)
        assert len(teams) == 5

    def test_list_teams_empty(self, team_manager):
        """Test listing teams when no teams exist"""
        teams = team_manager.list_teams()
        assert len(teams) == 0


class TestTeamMemberManagement:
    """Test team member management"""

    @pytest.fixture
    def sample_team(self, team_manager, sample_user):
        """Create a sample team for testing"""
        return team_manager.create_team(
            name="Test Team",
            description="A test team",
            owner_id=sample_user.id
        )

    def test_add_member_success(self, team_manager, sample_team, sample_user2):
        """Test successfully adding a member to a team"""
        member = team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.MEMBER)

        assert member.id is not None
        assert member.team_id == sample_team.id
        assert member.user_id == sample_user2.id
        assert member.role == TeamRole.MEMBER

    def test_add_member_with_admin_role(self, team_manager, sample_team, sample_user2):
        """Test adding a member with admin role"""
        member = team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.ADMIN)

        assert member.role == TeamRole.ADMIN

    def test_add_member_with_owner_role(self, team_manager, sample_team, sample_user2):
        """Test adding a member with owner role"""
        member = team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.OWNER)

        assert member.role == TeamRole.OWNER

    def test_add_member_nonexistent_team(self, team_manager, sample_user2):
        """Test adding member to non-existent team"""
        with pytest.raises(ValueError, match="Team with ID 99999 does not exist"):
            team_manager.add_member(99999, sample_user2.id, TeamRole.MEMBER)

    def test_add_member_nonexistent_user(self, team_manager, sample_team):
        """Test adding non-existent user to team"""
        with pytest.raises(ValueError, match="User with ID 99999 does not exist"):
            team_manager.add_member(sample_team.id, 99999, TeamRole.MEMBER)

    def test_add_member_duplicate(self, team_manager, sample_team, sample_user2):
        """Test adding duplicate member to team"""
        team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.MEMBER)

        with pytest.raises(ValueError, match=f"User {sample_user2.id} is already a member of team {sample_team.id}"):
            team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.ADMIN)

    def test_remove_member_success(self, team_manager, sample_team, sample_user2):
        """Test successfully removing a member from a team"""
        team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.MEMBER)

        result = team_manager.remove_member(sample_team.id, sample_user2.id)
        assert result is True

        # Verify member is removed
        members = team_manager.get_team_members(sample_team.id)
        assert len(members) == 0

    def test_remove_member_not_found(self, team_manager, sample_team, sample_user2):
        """Test removing non-existent member"""
        result = team_manager.remove_member(sample_team.id, sample_user2.id)
        assert result is False

    def test_update_member_role_success(self, team_manager, sample_team, sample_user2):
        """Test successfully updating member role"""
        team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.MEMBER)

        updated = team_manager.update_member_role(sample_team.id, sample_user2.id, TeamRole.ADMIN)
        assert updated is not None
        assert updated.role == TeamRole.ADMIN

    def test_update_member_role_not_found(self, team_manager, sample_team, sample_user2):
        """Test updating role of non-existent member"""
        result = team_manager.update_member_role(sample_team.id, sample_user2.id, TeamRole.ADMIN)
        assert result is None

    def test_get_team_members(self, team_manager, sample_team, sample_user, sample_user2, user_manager):
        """Test getting all team members"""
        # Create a third user
        user3 = user_manager.create_user(
            username="testuser3",
            email="test3@example.com",
            password="password123",
            full_name="Test User 3"
        )

        # Add members
        team_manager.add_member(sample_team.id, sample_user2.id, TeamRole.MEMBER)
        team_manager.add_member(sample_team.id, user3.id, TeamRole.ADMIN)

        members = team_manager.get_team_members(sample_team.id)
        assert len(members) == 2

        # Verify member details
        member_user_ids = [m.user_id for m in members]
        assert sample_user2.id in member_user_ids
        assert user3.id in member_user_ids

    def test_get_team_members_empty(self, team_manager, sample_team):
        """Test getting team members when team has no members"""
        members = team_manager.get_team_members(sample_team.id)
        assert len(members) == 0

    def test_get_user_teams(self, team_manager, sample_user, sample_user2):
        """Test getting all teams a user is a member of"""
        # Create multiple teams
        team1 = team_manager.create_team(
            name="Team 1",
            description="First team",
            owner_id=sample_user.id
        )
        team2 = team_manager.create_team(
            name="Team 2",
            description="Second team",
            owner_id=sample_user.id
        )
        team3 = team_manager.create_team(
            name="Team 3",
            description="Third team",
            owner_id=sample_user.id
        )

        # Add user2 to team1 and team3
        team_manager.add_member(team1.id, sample_user2.id, TeamRole.MEMBER)
        team_manager.add_member(team3.id, sample_user2.id, TeamRole.ADMIN)

        # Get user2's teams
        teams = team_manager.get_user_teams(sample_user2.id)
        assert len(teams) == 2

        team_ids = [t.id for t in teams]
        assert team1.id in team_ids
        assert team3.id in team_ids
        assert team2.id not in team_ids

    def test_get_user_teams_empty(self, team_manager, sample_user2):
        """Test getting user teams when user is not a member of any team"""
        teams = team_manager.get_user_teams(sample_user2.id)
        assert len(teams) == 0
