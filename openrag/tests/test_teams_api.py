"""Tests for Team Management API"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.main import app
from openrag.api.deps import get_db
from openrag.models.base import Base
from openrag.models.user import User
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.security import hash_password, create_access_token


# Test database setup
@pytest.fixture(scope="function")
def engine():
    """Create test database engine"""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture(scope="function")
def db(engine):
    """Create test database session"""
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        """Override database dependency for testing"""
        try:
            db = TestingSessionLocal()
            yield db
        finally:
            db.close()

    # Override dependencies
    app.dependency_overrides[get_db] = override_get_db

    db = TestingSessionLocal()
    yield db
    db.close()

    # Clean up override
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


@pytest.fixture
def test_user(db):
    """Create test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=hash_password("testpass123"),
        full_name="Test User",
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_user2(db):
    """Create second test user"""
    user = User(
        username="testuser2",
        email="test2@example.com",
        password_hash=hash_password("testpass123"),
        full_name="Test User 2",
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def auth_token(test_user):
    """Create authentication token for test user"""
    return create_access_token(data={"sub": str(test_user.id)})


@pytest.fixture
def auth_token2(test_user2):
    """Create authentication token for test user 2"""
    return create_access_token(data={"sub": str(test_user2.id)})


@pytest.fixture
def auth_headers(auth_token):
    """Create authorization headers"""
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
def auth_headers2(auth_token2):
    """Create authorization headers for user 2"""
    return {"Authorization": f"Bearer {auth_token2}"}


@pytest.fixture
def test_team(db, test_user):
    """Create test team"""
    team = Team(
        name="Test Team",
        description="Test team description",
        owner_id=test_user.id
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


class TestCreateTeam:
    """Test team creation endpoint"""

    def test_create_team_success(self, client, test_user, auth_headers):
        """Test successful team creation"""
        response = client.post(
            "/teams",
            headers=auth_headers,
            json={
                "name": "New Team",
                "description": "New team description"
            }
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["name"] == "New Team"
        assert data["description"] == "New team description"
        assert data["owner_id"] == test_user.id
        assert "id" in data

    def test_create_team_duplicate_name(self, client, test_team, auth_headers):
        """Test creating team with duplicate name"""
        response = client.post(
            "/teams",
            headers=auth_headers,
            json={
                "name": "Test Team",
                "description": "Another description"
            }
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in response.json()["detail"].lower()

    def test_create_team_no_token(self, client):
        """Test creating team without token"""
        response = client.post(
            "/teams",
            json={
                "name": "New Team",
                "description": "Description"
            }
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_create_team_missing_fields(self, client, auth_headers):
        """Test creating team with missing fields"""
        response = client.post(
            "/teams",
            headers=auth_headers,
            json={"name": "New Team"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestListTeams:
    """Test list teams endpoint"""

    def test_list_teams_success(self, client, db, test_user, test_team, auth_headers):
        """Test listing user's teams"""
        # Add user as member of the team
        member = TeamMember(
            team_id=test_team.id,
            user_id=test_user.id,
            role=TeamRole.OWNER
        )
        db.add(member)
        db.commit()

        response = client.get("/teams", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert any(team["id"] == test_team.id for team in data)

    def test_list_teams_empty(self, client, test_user, auth_headers):
        """Test listing teams when user has no teams"""
        response = client.get("/teams", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_list_teams_no_token(self, client):
        """Test listing teams without token"""
        response = client.get("/teams")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestGetTeam:
    """Test get team by ID endpoint"""

    def test_get_team_success(self, client, db, test_user, test_team, auth_headers):
        """Test getting team by ID"""
        # Add user as member
        member = TeamMember(
            team_id=test_team.id,
            user_id=test_user.id,
            role=TeamRole.OWNER
        )
        db.add(member)
        db.commit()

        response = client.get(f"/teams/{test_team.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == test_team.id
        assert data["name"] == test_team.name
        assert data["description"] == test_team.description

    def test_get_team_not_found(self, client, auth_headers):
        """Test getting non-existent team"""
        response = client.get("/teams/99999", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_team_not_member(self, client, test_team, auth_headers):
        """Test getting team when not a member"""
        response = client.get(f"/teams/{test_team.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_get_team_no_token(self, client, test_team):
        """Test getting team without token"""
        response = client.get(f"/teams/{test_team.id}")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestUpdateTeam:
    """Test update team endpoint"""

    def test_update_team_success(self, client, test_user, test_team, auth_headers):
        """Test updating team as owner"""
        response = client.put(
            f"/teams/{test_team.id}",
            headers=auth_headers,
            json={
                "name": "Updated Team",
                "description": "Updated description"
            }
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "Updated Team"
        assert data["description"] == "Updated description"

    def test_update_team_not_owner(self, client, db, test_user2, test_team, auth_headers2):
        """Test updating team as non-owner"""
        # Add user2 as member but not owner
        member = TeamMember(
            team_id=test_team.id,
            user_id=test_user2.id,
            role=TeamRole.MEMBER
        )
        db.add(member)
        db.commit()

        response = client.put(
            f"/teams/{test_team.id}",
            headers=auth_headers2,
            json={"name": "Updated Team"}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_team_not_found(self, client, auth_headers):
        """Test updating non-existent team"""
        response = client.put(
            "/teams/99999",
            headers=auth_headers,
            json={"name": "Updated Team"}
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_team_no_token(self, client, test_team):
        """Test updating team without token"""
        response = client.put(
            f"/teams/{test_team.id}",
            json={"name": "Updated Team"}
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestDeleteTeam:
    """Test delete team endpoint"""

    def test_delete_team_success(self, client, test_user, test_team, auth_headers):
        """Test deleting team as owner"""
        response = client.delete(f"/teams/{test_team.id}", headers=auth_headers)

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_team_not_owner(self, client, db, test_user2, test_team, auth_headers2):
        """Test deleting team as non-owner"""
        # Add user2 as member but not owner
        member = TeamMember(
            team_id=test_team.id,
            user_id=test_user2.id,
            role=TeamRole.MEMBER
        )
        db.add(member)
        db.commit()

        response = client.delete(f"/teams/{test_team.id}", headers=auth_headers2)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_team_not_found(self, client, auth_headers):
        """Test deleting non-existent team"""
        response = client.delete("/teams/99999", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_team_no_token(self, client, test_team):
        """Test deleting team without token"""
        response = client.delete(f"/teams/{test_team.id}")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestAddTeamMember:
    """Test add team member endpoint"""

    def test_add_member_success(self, client, test_user, test_user2, test_team, auth_headers):
        """Test adding member as owner"""
        response = client.post(
            f"/teams/{test_team.id}/members",
            headers=auth_headers,
            json={
                "user_id": test_user2.id,
                "role": "member"
            }
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["team_id"] == test_team.id
        assert data["user_id"] == test_user2.id
        assert data["role"] == "member"

    def test_add_member_not_owner(self, client, db, test_user2, test_team, auth_headers2):
        """Test adding member as non-owner"""
        response = client.post(
            f"/teams/{test_team.id}/members",
            headers=auth_headers2,
            json={
                "user_id": test_user2.id,
                "role": "member"
            }
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_add_member_already_exists(self, client, db, test_user, test_user2, test_team, auth_headers):
        """Test adding member that already exists"""
        # Add member first
        member = TeamMember(
            team_id=test_team.id,
            user_id=test_user2.id,
            role=TeamRole.MEMBER
        )
        db.add(member)
        db.commit()

        response = client.post(
            f"/teams/{test_team.id}/members",
            headers=auth_headers,
            json={
                "user_id": test_user2.id,
                "role": "member"
            }
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_add_member_team_not_found(self, client, test_user2, auth_headers):
        """Test adding member to non-existent team"""
        response = client.post(
            "/teams/99999/members",
            headers=auth_headers,
            json={
                "user_id": test_user2.id,
                "role": "member"
            }
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_add_member_user_not_found(self, client, test_team, auth_headers):
        """Test adding non-existent user"""
        response = client.post(
            f"/teams/{test_team.id}/members",
            headers=auth_headers,
            json={
                "user_id": 99999,
                "role": "member"
            }
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestRemoveTeamMember:
    """Test remove team member endpoint"""

    def test_remove_member_success(self, client, db, test_user, test_user2, test_team, auth_headers):
        """Test removing member as owner"""
        # Add member first
        member = TeamMember(
            team_id=test_team.id,
            user_id=test_user2.id,
            role=TeamRole.MEMBER
        )
        db.add(member)
        db.commit()

        response = client.delete(
            f"/teams/{test_team.id}/members/{test_user2.id}",
            headers=auth_headers
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_remove_member_not_owner(self, client, db, test_user, test_user2, test_team, auth_headers2):
        """Test removing member as non-owner"""
        # Add user2 as member
        member = TeamMember(
            team_id=test_team.id,
            user_id=test_user2.id,
            role=TeamRole.MEMBER
        )
        db.add(member)
        db.commit()

        response = client.delete(
            f"/teams/{test_team.id}/members/{test_user.id}",
            headers=auth_headers2
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_remove_member_not_found(self, client, test_team, auth_headers):
        """Test removing non-existent member"""
        response = client.delete(
            f"/teams/{test_team.id}/members/99999",
            headers=auth_headers
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_remove_owner(self, client, test_user, test_team, auth_headers):
        """Test removing team owner (should fail)"""
        response = client.delete(
            f"/teams/{test_team.id}/members/{test_user.id}",
            headers=auth_headers
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "owner" in response.json()["detail"].lower()


class TestListTeamMembers:
    """Test list team members endpoint"""

    def test_list_members_success(self, client, db, test_user, test_user2, test_team, auth_headers):
        """Test listing team members"""
        # Add members
        member1 = TeamMember(
            team_id=test_team.id,
            user_id=test_user.id,
            role=TeamRole.OWNER
        )
        member2 = TeamMember(
            team_id=test_team.id,
            user_id=test_user2.id,
            role=TeamRole.MEMBER
        )
        db.add(member1)
        db.add(member2)
        db.commit()

        response = client.get(f"/teams/{test_team.id}/members", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_list_members_not_member(self, client, test_team, auth_headers):
        """Test listing members when not a team member"""
        response = client.get(f"/teams/{test_team.id}/members", headers=auth_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_list_members_team_not_found(self, client, auth_headers):
        """Test listing members of non-existent team"""
        response = client.get("/teams/99999/members", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
