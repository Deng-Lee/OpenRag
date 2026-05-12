"""Permission filtering for retrieval results"""

from typing import Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.models.file import File


class PermissionFilter:
    """Filter retrieval results based on workspace permissions"""

    def __init__(self, db: Session):
        """
        Initialize PermissionFilter with database session

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def get_accessible_uris(self, user_id: int) -> Set[str]:
        """
        Get all URIs (VikingFS paths) that a user can access

        This includes files from workspaces where user has read permission

        Args:
            user_id: User ID

        Returns:
            Set of accessible URIs
        """
        from openrag.services.workspace_service import WorkspaceService
        from openrag.models.user import User

        accessible_uris = set()

        # Check if user is admin (can access all files)
        user = self.db.query(User).filter(User.id == user_id).first()
        if user and user.is_admin:
            all_files = self.db.execute(select(File)).scalars().all()
            for file in all_files:
                accessible_uris.add(file.uri)
            return accessible_uris

        # Get workspaces user has read access to
        ws_service = WorkspaceService(self.db)
        accessible_workspaces = ws_service.get_user_workspaces_with_permission(
            user_id, permission='read'
        )
        workspace_ids = [ws.id for ws in accessible_workspaces]

        if not workspace_ids:
            return accessible_uris

        # Get files from accessible workspaces
        files = self.db.execute(
            select(File).where(File.workspace_id.in_(workspace_ids))
        ).scalars().all()

        for file in files:
            accessible_uris.add(file.uri)

        return accessible_uris

    def filter_results(self, results: list[dict], accessible_uris: Set[str]) -> list[dict]:
        """
        Filter retrieval results to only include accessible URIs

        Args:
            results: List of retrieval results with 'uri' field
            accessible_uris: Set of URIs user can access

        Returns:
            Filtered list of results
        """
        return [result for result in results if result.get("uri") in accessible_uris]
