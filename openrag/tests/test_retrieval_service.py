"""Tests for retrieval service with permission filtering"""

import pytest
from unittest.mock import Mock, MagicMock
from sqlalchemy.orm import Session

from openrag.models.file import File
from openrag.models.permission import EntityType, FilePermission, Permission
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.user import User
from openrag.retrieval.filters import PermissionFilter
from openrag.retrieval.retrieval_service import RetrievalService


@pytest.fixture
def db_session(tmp_path):
    """Create in-memory SQLite database for testing"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from openrag.models.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()


@pytest.fixture
def test_users(db_session):
    """Create test users"""
    user1 = User(username="alice", email="alice@example.com", password_hash="hash1", full_name="Alice")
    user2 = User(username="bob", email="bob@example.com", password_hash="hash2", full_name="Bob")
    user3 = User(username="charlie", email="charlie@example.com", password_hash="hash3", full_name="Charlie")

    db_session.add_all([user1, user2, user3])
    db_session.commit()

    return {"alice": user1, "bob": user2, "charlie": user3}


@pytest.fixture
def test_team(db_session, test_users):
    """Create test team with members"""
    team = Team(name="Engineering", description="Engineering team", owner_id=test_users["alice"].id)
    db_session.add(team)
    db_session.commit()

    # Add bob as team member
    member = TeamMember(team_id=team.id, user_id=test_users["bob"].id, role=TeamRole.MEMBER)
    db_session.add(member)
    db_session.commit()

    return team


@pytest.fixture
def test_files(db_session, test_users):
    """Create test files with different owners"""
    file1 = File(
        uri="viking://bucket/file1.pdf",
        name="file1.pdf",
        owner_id=test_users["alice"].id,
        size=1024,
        mime_type="application/pdf"
    )
    file2 = File(
        uri="viking://bucket/file2.pdf",
        name="file2.pdf",
        owner_id=test_users["bob"].id,
        size=2048,
        mime_type="application/pdf"
    )
    file3 = File(
        uri="viking://bucket/file3.pdf",
        name="file3.pdf",
        owner_id=test_users["charlie"].id,
        size=3072,
        mime_type="application/pdf"
    )

    db_session.add_all([file1, file2, file3])
    db_session.commit()

    return {"file1": file1, "file2": file2, "file3": file3}


@pytest.fixture
def test_permissions(db_session, test_users, test_team, test_files):
    """Create test permissions"""
    # Alice shares file1 with bob (read)
    perm1 = FilePermission(
        file_id=test_files["file1"].id,
        entity_type=EntityType.USER,
        entity_id=test_users["bob"].id,
        permission=Permission.READ
    )

    # Charlie shares file3 with Engineering team (write)
    perm2 = FilePermission(
        file_id=test_files["file3"].id,
        entity_type=EntityType.TEAM,
        entity_id=test_team.id,
        permission=Permission.WRITE
    )

    db_session.add_all([perm1, perm2])
    db_session.commit()

    return [perm1, perm2]


class TestPermissionFilter:
    """Test PermissionFilter class"""

    def test_get_accessible_uris_owner(self, db_session, test_users, test_files):
        """Test getting accessible URIs for file owner"""
        filter = PermissionFilter(db_session)
        uris = filter.get_accessible_uris(test_users["alice"].id)

        assert "viking://bucket/file1.pdf" in uris
        assert "viking://bucket/file2.pdf" not in uris
        assert "viking://bucket/file3.pdf" not in uris

    def test_get_accessible_uris_with_direct_permission(
        self, db_session, test_users, test_files, test_permissions
    ):
        """Test getting accessible URIs with direct user permission"""
        filter = PermissionFilter(db_session)
        uris = filter.get_accessible_uris(test_users["bob"].id)

        # Bob owns file2 and has read permission on file1
        assert "viking://bucket/file1.pdf" in uris
        assert "viking://bucket/file2.pdf" in uris
        assert "viking://bucket/file3.pdf" in uris  # Through team permission

    def test_get_accessible_uris_with_team_permission(
        self, db_session, test_users, test_team, test_files, test_permissions
    ):
        """Test getting accessible URIs through team membership"""
        filter = PermissionFilter(db_session)
        uris = filter.get_accessible_uris(test_users["bob"].id)

        # Bob is in Engineering team which has access to file3
        assert "viking://bucket/file3.pdf" in uris

    def test_get_accessible_uris_no_permissions(self, db_session, test_users, test_files):
        """Test getting accessible URIs for user with no permissions"""
        filter = PermissionFilter(db_session)
        uris = filter.get_accessible_uris(test_users["charlie"].id)

        # Charlie only owns file3
        assert "viking://bucket/file1.pdf" not in uris
        assert "viking://bucket/file2.pdf" not in uris
        assert "viking://bucket/file3.pdf" in uris

    def test_filter_results(self, db_session):
        """Test filtering results by accessible URIs"""
        filter = PermissionFilter(db_session)

        results = [
            {"uri": "viking://bucket/file1.pdf", "text": "content1", "score": 0.9},
            {"uri": "viking://bucket/file2.pdf", "text": "content2", "score": 0.8},
            {"uri": "viking://bucket/file3.pdf", "text": "content3", "score": 0.7},
        ]

        accessible_uris = {"viking://bucket/file1.pdf", "viking://bucket/file3.pdf"}
        filtered = filter.filter_results(results, accessible_uris)

        assert len(filtered) == 2
        assert filtered[0]["uri"] == "viking://bucket/file1.pdf"
        assert filtered[1]["uri"] == "viking://bucket/file3.pdf"

    def test_filter_results_empty_accessible(self, db_session):
        """Test filtering with no accessible URIs"""
        filter = PermissionFilter(db_session)

        results = [
            {"uri": "viking://bucket/file1.pdf", "text": "content1", "score": 0.9},
        ]

        filtered = filter.filter_results(results, set())
        assert len(filtered) == 0


class TestRetrievalService:
    """Test RetrievalService class"""

    def test_init_without_agfs_client(self, db_session):
        """Test initialization without AGFS client"""
        service = RetrievalService(db_session)
        assert service.db == db_session
        assert service.agfs_client is None

    def test_init_with_agfs_client(self, db_session):
        """Test initialization with AGFS client"""
        mock_client = Mock()
        service = RetrievalService(db_session, agfs_client=mock_client)
        assert service.agfs_client == mock_client

    def test_search_without_agfs_raises_error(self, db_session, test_users):
        """Test search without AGFS client raises error"""
        service = RetrievalService(db_session)

        with pytest.raises(RuntimeError, match="AGFS client not initialized"):
            service.search("test query", test_users["alice"].id)

    def test_search_semantic(
        self, db_session, test_users, test_files, test_permissions
    ):
        """Test semantic search with permission filtering"""
        # Mock AGFS client
        mock_client = Mock()
        mock_client.semantic_search.return_value = [
            {
                "text": "content from file1",
                "score": 0.9,
                "uri": "viking://bucket/file1.pdf",
                "page": 1,
                "offset": 0,
                "bbox": [0, 0, 100, 100],
                "level": 0
            },
            {
                "text": "content from file2",
                "score": 0.8,
                "uri": "viking://bucket/file2.pdf",
                "page": 1,
                "offset": 0,
                "bbox": [0, 0, 100, 100],
                "level": 0
            },
            {
                "text": "content from file3",
                "score": 0.7,
                "uri": "viking://bucket/file3.pdf",
                "page": 1,
                "offset": 0,
                "bbox": [0, 0, 100, 100],
                "level": 0
            }
        ]

        service = RetrievalService(db_session, agfs_client=mock_client)
        results = service.search("test query", test_users["bob"].id, top_k=10)

        # Bob should see file1 (shared), file2 (owned), file3 (team)
        assert len(results) == 3
        assert mock_client.semantic_search.called

    def test_search_hierarchical(
        self, db_session, test_users, test_files, test_permissions
    ):
        """Test hierarchical search with permission filtering"""
        # Mock AGFS client
        mock_client = Mock()
        mock_client.hierarchical_search.return_value = [
            {
                "text": "heading from file1",
                "score": 0.95,
                "uri": "viking://bucket/file1.pdf",
                "page": 1,
                "offset": 0,
                "bbox": [0, 0, 100, 100],
                "level": 1
            }
        ]

        service = RetrievalService(db_session, agfs_client=mock_client)
        results = service.search(
            "test query",
            test_users["alice"].id,
            top_k=10,
            use_hierarchical=True
        )

        assert len(results) == 1
        assert results[0]["level"] == 1
        assert mock_client.hierarchical_search.called

    def test_search_filters_by_permission(
        self, db_session, test_users, test_files
    ):
        """Test that search properly filters results by user permissions"""
        # Mock AGFS client returning results from all files
        mock_client = Mock()
        mock_client.semantic_search.return_value = [
            {
                "text": "content from file1",
                "score": 0.9,
                "uri": "viking://bucket/file1.pdf",
                "page": 1,
                "offset": 0,
                "bbox": None,
                "level": 0
            },
            {
                "text": "content from file2",
                "score": 0.8,
                "uri": "viking://bucket/file2.pdf",
                "page": 1,
                "offset": 0,
                "bbox": None,
                "level": 0
            }
        ]

        service = RetrievalService(db_session, agfs_client=mock_client)
        # Charlie should only see file3 (owned), not file1 or file2
        results = service.search("test query", test_users["charlie"].id, top_k=10)

        assert len(results) == 0  # No accessible files in results

    def test_search_with_file_id_mapping(
        self, db_session, test_users, test_files, test_permissions
    ):
        """Test that search includes file_id in results"""
        mock_client = Mock()
        mock_client.semantic_search.return_value = [
            {
                "text": "content from file1",
                "score": 0.9,
                "uri": "viking://bucket/file1.pdf",
                "page": 1,
                "offset": 0,
                "bbox": [0, 0, 100, 100],
                "level": 0
            }
        ]

        service = RetrievalService(db_session, agfs_client=mock_client)
        results = service.search("test query", test_users["alice"].id, top_k=10)

        assert len(results) == 1
        assert "file_id" in results[0]
        assert results[0]["file_id"] == test_files["file1"].id

    def test_search_empty_results(self, db_session, test_users):
        """Test search with no results from AGFS"""
        mock_client = Mock()
        mock_client.semantic_search.return_value = []

        service = RetrievalService(db_session, agfs_client=mock_client)
        results = service.search("test query", test_users["alice"].id, top_k=10)

        assert len(results) == 0
