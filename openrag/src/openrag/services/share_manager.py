"""Share link management service"""

import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.models.file import File
from openrag.models.share import ShareLink
from openrag.security import hash_password, verify_password


class ShareLinkManager:
    """Manager for share link operations"""

    def __init__(self, db: Session):
        """
        Initialize ShareLinkManager

        Args:
            db: Database session
        """
        self.db = db

    def _generate_unique_token(self) -> str:
        """
        Generate a unique token for share link

        Returns:
            Unique token string
        """
        max_attempts = 10
        for _ in range(max_attempts):
            token = secrets.token_urlsafe(32)
            # Check if token already exists
            stmt = select(ShareLink).where(ShareLink.token == token)
            existing = self.db.execute(stmt).scalar_one_or_none()
            if not existing:
                return token
        raise RuntimeError("Failed to generate unique token after multiple attempts")

    def create_share_link(
        self,
        file_id: int,
        created_by: int,
        password: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        max_access_count: Optional[int] = None
    ) -> ShareLink:
        """
        Create a share link for a file

        Args:
            file_id: File ID to share
            created_by: User ID who creates the share link
            password: Optional password for protection
            expires_at: Optional expiration timestamp
            max_access_count: Optional maximum access count

        Returns:
            Created ShareLink object

        Raises:
            ValueError: If file does not exist
        """
        # Validate file exists (active only: cannot share a soft-deleted file)
        stmt = select(File).where(File.id == file_id, File.deleted_at.is_(None))
        file = self.db.execute(stmt).scalar_one_or_none()
        if not file:
            raise ValueError(f"File with id {file_id} not found")

        # Generate unique token
        token = self._generate_unique_token()

        # Hash password if provided
        password_hash = None
        if password:
            password_hash = hash_password(password)

        # Create share link
        share_link = ShareLink(
            file_id=file_id,
            token=token,
            password_hash=password_hash,
            expires_at=expires_at,
            max_access_count=max_access_count,
            access_count=0,
            created_by=created_by
        )

        self.db.add(share_link)
        self.db.commit()
        self.db.refresh(share_link)

        return share_link

    def get_share_link(self, token: str) -> Optional[ShareLink]:
        """
        Get share link by token

        Args:
            token: Share link token

        Returns:
            ShareLink object or None if not found
        """
        stmt = select(ShareLink).where(ShareLink.token == token)
        return self.db.execute(stmt).scalar_one_or_none()

    def verify_share_link(
        self,
        token: str,
        password: Optional[str] = None
    ) -> tuple[Optional[ShareLink], Optional[str]]:
        """
        Verify share link access

        Args:
            token: Share link token
            password: Optional password for verification

        Returns:
            Tuple of (ShareLink, error_message)
            - ShareLink: The share link object if valid, None otherwise
            - error_message: None on success, error code on failure
                - "not_found": Link does not exist
                - "expired": Link has expired
                - "max_access_reached": Maximum access count reached
                - "invalid_password": Password is incorrect
        """
        # Check if link exists
        share_link = self.get_share_link(token)
        if not share_link:
            return None, "not_found"

        # Check if expired
        if share_link.expires_at:
            # Handle both timezone-aware and naive datetimes
            now = datetime.now(timezone.utc)
            expires_at = share_link.expires_at
            if expires_at.tzinfo is None:
                # If stored datetime is naive, compare with naive datetime
                now = datetime.now(timezone.utc).replace(tzinfo=None)
            if expires_at < now:
                return None, "expired"

        # Check if max access count reached
        if share_link.max_access_count is not None:
            if share_link.access_count >= share_link.max_access_count:
                return None, "max_access_reached"

        # Verify password if required
        if share_link.password_hash:
            if not password:
                return None, "invalid_password"
            if not verify_password(password, share_link.password_hash):
                return None, "invalid_password"

        return share_link, None

    def access_share_link(
        self,
        token: str,
        password: Optional[str] = None
    ) -> tuple[Optional[File], Optional[str]]:
        """
        Access a share link and increment access count

        Args:
            token: Share link token
            password: Optional password for verification

        Returns:
            Tuple of (File, error_message)
            - File: The file object if access is granted, None otherwise
            - error_message: None on success, error code on failure
        """
        # Verify share link
        share_link, error = self.verify_share_link(token, password)
        if error:
            return None, error

        # Increment access count
        share_link.access_count += 1
        self.db.commit()

        # Get and return the file (Codex round-4: hide soft-deleted/removed files from
        # public share access; also avoids a 500 when the file row is already gone).
        stmt = select(File).where(File.id == share_link.file_id, File.deleted_at.is_(None))
        file = self.db.execute(stmt).scalar_one_or_none()
        if file is None:
            return None, "not_found"

        return file, None

    def revoke_share_link(self, token: str) -> bool:
        """
        Revoke (delete) a share link

        Args:
            token: Share link token

        Returns:
            True if deleted, False if not found
        """
        share_link = self.get_share_link(token)
        if not share_link:
            return False

        self.db.delete(share_link)
        self.db.commit()

        return True

    def list_file_share_links(self, file_id: int) -> list[ShareLink]:
        """
        List all share links for a file

        Args:
            file_id: File ID

        Returns:
            List of ShareLink objects
        """
        stmt = select(ShareLink).where(ShareLink.file_id == file_id)
        result = self.db.execute(stmt).scalars().all()
        return list(result)

    def update_share_link(
        self,
        token: str,
        password: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        max_access_count: Optional[int] = None
    ) -> Optional[ShareLink]:
        """
        Update share link settings

        Args:
            token: Share link token
            password: New password (will be hashed)
            expires_at: New expiration timestamp
            max_access_count: New maximum access count

        Returns:
            Updated ShareLink object or None if not found
        """
        share_link = self.get_share_link(token)
        if not share_link:
            return None

        # Update fields if provided
        if password is not None:
            share_link.password_hash = hash_password(password)

        if expires_at is not None:
            share_link.expires_at = expires_at

        if max_access_count is not None:
            share_link.max_access_count = max_access_count

        self.db.commit()
        self.db.refresh(share_link)

        return share_link
