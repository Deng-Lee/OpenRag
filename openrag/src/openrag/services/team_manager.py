"""Team management service - handles team CRUD operations and member management"""

from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.user import User


class TeamManager:
    """Team management service for CRUD operations and member management"""

    def __init__(self, session: Session):
        """
        Initialize TeamManager with database session

        Args:
            session: SQLAlchemy database session
        """
        self.session = session

    def create_team(self, name: str, description: str, owner_id: int) -> Team:
        """
        Create a new team

        Args:
            name: Unique team name
            description: Team description
            owner_id: User ID of team owner

        Returns:
            Created Team object

        Raises:
            ValueError: If team name already exists, owner doesn't exist, or invalid input
        """
        if not name or not name.strip():
            raise ValueError("Team name cannot be empty")

        # Validate owner exists
        owner = self.session.query(User).filter(User.id == owner_id).first()
        if not owner:
            raise ValueError(f"Owner user with ID {owner_id} does not exist")

        # Check for existing team name
        existing = self.session.query(Team).filter(Team.name == name).first()
        if existing:
            raise ValueError(f"Team name '{name}' already exists")

        # Create team
        team = Team(
            name=name,
            description=description,
            owner_id=owner_id
        )

        try:
            self.session.add(team)
            self.session.commit()
            self.session.refresh(team)
            return team
        except IntegrityError as e:
            self.session.rollback()
            raise ValueError(f"Database integrity error: {str(e)}")

    def get_team_by_id(self, team_id: int) -> Optional[Team]:
        """
        Get team by ID

        Args:
            team_id: Team ID

        Returns:
            Team object or None if not found
        """
        return self.session.query(Team).filter(Team.id == team_id).first()

    def get_team_by_name(self, name: str) -> Optional[Team]:
        """
        Get team by name

        Args:
            name: Team name

        Returns:
            Team object or None if not found
        """
        return self.session.query(Team).filter(Team.name == name).first()

    def update_team(self, team_id: int, **kwargs) -> Optional[Team]:
        """
        Update team with partial or full data

        Args:
            team_id: Team ID
            **kwargs: Fields to update (name, description)

        Returns:
            Updated Team object or None if not found

        Raises:
            ValueError: If team name already exists or invalid input
        """
        team = self.get_team_by_id(team_id)
        if not team:
            return None

        # Validate and check for duplicates
        if 'name' in kwargs:
            new_name = kwargs['name']
            if not new_name or not new_name.strip():
                raise ValueError("Team name cannot be empty")
            if new_name != team.name:
                existing = self.get_team_by_name(new_name)
                if existing:
                    raise ValueError(f"Team name '{new_name}' already exists")
                team.name = new_name

        if 'description' in kwargs:
            team.description = kwargs['description']

        try:
            self.session.commit()
            self.session.refresh(team)
            return team
        except IntegrityError as e:
            self.session.rollback()
            raise ValueError(f"Database integrity error: {str(e)}")

    def delete_team(self, team_id: int) -> bool:
        """
        Delete team (hard delete, cascades to team members)

        Args:
            team_id: Team ID

        Returns:
            True if deleted, False if team not found
        """
        team = self.get_team_by_id(team_id)
        if not team:
            return False

        try:
            self.session.delete(team)
            self.session.commit()
            return True
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to delete team: {str(e)}")

    def list_teams(self, skip: int = 0, limit: int = 100) -> list[Team]:
        """
        List teams with pagination

        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of Team objects
        """
        return self.session.query(Team).offset(skip).limit(limit).all()

    def add_member(self, team_id: int, user_id: int, role: TeamRole) -> TeamMember:
        """
        Add a member to a team

        Args:
            team_id: Team ID
            user_id: User ID
            role: Team role (owner, admin, member)

        Returns:
            Created TeamMember object

        Raises:
            ValueError: If team/user doesn't exist or member already exists
        """
        # Validate team exists
        team = self.get_team_by_id(team_id)
        if not team:
            raise ValueError(f"Team with ID {team_id} does not exist")

        # Validate user exists
        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"User with ID {user_id} does not exist")

        # Check for existing membership
        existing = self.session.query(TeamMember).filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == user_id
        ).first()
        if existing:
            raise ValueError(f"User {user_id} is already a member of team {team_id}")

        # Create team member
        member = TeamMember(
            team_id=team_id,
            user_id=user_id,
            role=role
        )

        try:
            self.session.add(member)
            self.session.commit()
            self.session.refresh(member)
            return member
        except IntegrityError as e:
            self.session.rollback()
            raise ValueError(f"Database integrity error: {str(e)}")

    def remove_member(self, team_id: int, user_id: int) -> bool:
        """
        Remove a member from a team

        Args:
            team_id: Team ID
            user_id: User ID

        Returns:
            True if removed, False if membership not found
        """
        member = self.session.query(TeamMember).filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == user_id
        ).first()

        if not member:
            return False

        try:
            self.session.delete(member)
            self.session.commit()
            return True
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to remove member: {str(e)}")

    def update_member_role(self, team_id: int, user_id: int, role: TeamRole) -> Optional[TeamMember]:
        """
        Update a team member's role

        Args:
            team_id: Team ID
            user_id: User ID
            role: New team role

        Returns:
            Updated TeamMember object or None if not found
        """
        member = self.session.query(TeamMember).filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == user_id
        ).first()

        if not member:
            return None

        member.role = role

        try:
            self.session.commit()
            self.session.refresh(member)
            return member
        except Exception as e:
            self.session.rollback()
            raise ValueError(f"Failed to update member role: {str(e)}")

    def get_team_members(self, team_id: int) -> list[TeamMember]:
        """
        Get all members of a team

        Args:
            team_id: Team ID

        Returns:
            List of TeamMember objects
        """
        return self.session.query(TeamMember).filter(
            TeamMember.team_id == team_id
        ).all()

    def get_user_teams(self, user_id: int) -> list[Team]:
        """
        Get all teams a user is a member of

        Args:
            user_id: User ID

        Returns:
            List of Team objects
        """
        team_ids = self.session.query(TeamMember.team_id).filter(
            TeamMember.user_id == user_id
        ).all()

        if not team_ids:
            return []

        team_id_list = [tid[0] for tid in team_ids]
        return self.session.query(Team).filter(Team.id.in_(team_id_list)).all()
