"""Integration tests for complete retrieval pipeline.

Tests the end-to-end flow: RetrievalService → PermissionFilter → Reranker → Search API
"""

import pytest
from unittest.mock import Mock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from fastapi.testclient import TestClient
from fastapi import FastAPI

from openrag.models.base import Base
from openrag.models.user import User
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.file import File
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.retrieval.retrieval_service import RetrievalService
from openrag.retrieval.reranker import Reranker
from openrag.api.deps import get_current_active_user as get_current_user
from openrag.api.search_api import router
from openrag.database import get_db


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def db_session():
    """Create in-memory SQLite database for testing"""
    # Use check_same_thread=False for SQLite to work with FastAPI TestClient
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()


@pytest.fixture
def test_users(db_session):
    """Create test users with different roles"""
    alice = User(
        username="alice",
        email="alice@example.com",
        password_hash="hash_alice",
        full_name="Alice Smith"
    )
    bob = User(
        username="bob",
        email="bob@example.com",
        password_hash="hash_bob",
        full_name="Bob Johnson"
    )
    charlie = User(
        username="charlie",
        email="charlie@example.com",
        password_hash="hash_charlie",
        full_name="Charlie Brown"
    )
    diana = User(
        username="diana",
        email="diana@example.com",
        password_hash="hash_diana",
        full_name="Diana Prince"
    )

    db_session.add_all([alice, bob, charlie, diana])
    db_session.commit()

    return {
        "alice": alice,
        "bob": bob,
        "charlie": charlie,
        "diana": diana
    }


@pytest.fixture
def test_workspaces(db_session, test_users):
    workspaces = {
        name: Workspace(
            name=f"{name.title()} Workspace",
            slug=f"{name}-workspace",
            owner_id=test_users["alice"].id,
        )
        for name in ("ml", "database", "security", "private")
    }
    db_session.add_all(workspaces.values())
    db_session.commit()
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=workspaces["ml"].id,
                user_id=test_users["alice"].id,
                role="write",
            ),
            WorkspaceMember(
                workspace_id=workspaces["ml"].id,
                user_id=test_users["bob"].id,
                role="read",
            ),
            WorkspaceMember(
                workspace_id=workspaces["database"].id,
                user_id=test_users["bob"].id,
                role="write",
            ),
            WorkspaceMember(
                workspace_id=workspaces["security"].id,
                user_id=test_users["charlie"].id,
                role="write",
            ),
            WorkspaceMember(
                workspace_id=workspaces["security"].id,
                user_id=test_users["alice"].id,
                role="read",
            ),
            WorkspaceMember(
                workspace_id=workspaces["private"].id,
                user_id=test_users["diana"].id,
                role="write",
            ),
        ]
    )
    db_session.commit()
    return workspaces


@pytest.fixture
def test_team(db_session, test_users, test_workspaces):
    """Create test team with members"""
    team = Team(
        name="Engineering",
        description="Engineering team for collaboration",
        owner_id=test_users["alice"].id,
        workspace_id=test_workspaces["database"].id,
    )
    db_session.add(team)
    db_session.commit()

    # Add bob and charlie as team members
    bob_member = TeamMember(
        team_id=team.id,
        user_id=test_users["bob"].id,
        role=TeamRole.MEMBER
    )
    charlie_member = TeamMember(
        team_id=team.id,
        user_id=test_users["charlie"].id,
        role=TeamRole.MEMBER
    )

    db_session.add_all([bob_member, charlie_member])
    db_session.commit()

    return team


@pytest.fixture
def test_files(db_session, test_users, test_workspaces):
    """Create files bound to explicit workspace authorization scopes."""
    # Alice's files - Machine Learning topics
    file1 = File(
        uri="viking://bucket/ml_intro.pdf",
        name="ml_intro.pdf",
        owner_id=test_users["alice"].id,
        workspace_id=test_workspaces["ml"].id,
        size=10240,
        mime_type="application/pdf"
    )
    file2 = File(
        uri="viking://bucket/deep_learning.pdf",
        name="deep_learning.pdf",
        owner_id=test_users["alice"].id,
        workspace_id=test_workspaces["ml"].id,
        size=20480,
        mime_type="application/pdf"
    )

    # Bob's file - Database topics
    file3 = File(
        uri="viking://bucket/databases.pdf",
        name="databases.pdf",
        owner_id=test_users["bob"].id,
        workspace_id=test_workspaces["database"].id,
        size=15360,
        mime_type="application/pdf"
    )

    # Charlie's file - Security topics
    file4 = File(
        uri="viking://bucket/security.pdf",
        name="security.pdf",
        owner_id=test_users["charlie"].id,
        workspace_id=test_workspaces["security"].id,
        size=12288,
        mime_type="application/pdf"
    )

    # Diana's file - No sharing
    file5 = File(
        uri="viking://bucket/private.pdf",
        name="private.pdf",
        owner_id=test_users["diana"].id,
        workspace_id=test_workspaces["private"].id,
        size=8192,
        mime_type="application/pdf"
    )

    db_session.add_all([file1, file2, file3, file4, file5])
    db_session.commit()

    return {
        "ml_intro": file1,
        "deep_learning": file2,
        "databases": file3,
        "security": file4,
        "private": file5
    }

@pytest.fixture
def mock_agfs_client():
    """Create mock OpenViking AGFS client with realistic search results"""
    client = Mock()

    # Mock semantic search results
    def semantic_search(query, top_k=10):
        query_lower = query.lower()

        # Machine learning query
        if "machine learning" in query_lower or "ml" in query_lower:
            return [
                {
                    "text": "Machine learning is a subset of artificial intelligence.",
                    "score": 0.95,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": [100.0, 200.0, 500.0, 250.0],
                    "level": 1,
                    "block_type": "heading"
                },
                {
                    "text": "Deep learning uses neural networks with multiple layers.",
                    "score": 0.88,
                    "uri": "viking://bucket/deep_learning.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": [100.0, 300.0, 500.0, 350.0],
                    "level": 1,
                    "block_type": "heading"
                },
                {
                    "text": "Database systems store and retrieve data efficiently.",
                    "score": 0.45,
                    "uri": "viking://bucket/databases.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                }
            ]

        # Database query
        elif "database" in query_lower or "sql" in query_lower:
            return [
                {
                    "text": "SQL databases use structured query language.",
                    "score": 0.92,
                    "uri": "viking://bucket/databases.pdf",
                    "page": 2,
                    "offset": 500,
                    "bbox": [100.0, 100.0, 500.0, 150.0],
                    "level": 2,
                    "block_type": "heading"
                },
                {
                    "text": "NoSQL databases provide flexible schema design.",
                    "score": 0.85,
                    "uri": "viking://bucket/databases.pdf",
                    "page": 3,
                    "offset": 1000,
                    "bbox": [100.0, 200.0, 500.0, 250.0],
                    "level": 2,
                    "block_type": "heading"
                }
            ]

        # Security query
        elif "security" in query_lower or "encryption" in query_lower:
            return [
                {
                    "text": "Encryption protects data from unauthorized access.",
                    "score": 0.90,
                    "uri": "viking://bucket/security.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": [100.0, 150.0, 500.0, 200.0],
                    "level": 1,
                    "block_type": "title"
                },
                {
                    "text": "Private information must be secured properly.",
                    "score": 0.75,
                    "uri": "viking://bucket/private.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                }
            ]

        # Default - return empty
        return []

    # Mock hierarchical search results
    def hierarchical_search(query, top_k=10):
        query_lower = query.lower()

        if "introduction" in query_lower:
            return [
                {
                    "text": "Introduction to Machine Learning",
                    "score": 0.98,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": [50.0, 50.0, 550.0, 100.0],
                    "level": 1,
                    "block_type": "title"
                },
                {
                    "text": "Chapter 1: Basic Concepts",
                    "score": 0.85,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 2,
                    "offset": 0,
                    "bbox": [50.0, 50.0, 550.0, 100.0],
                    "level": 1,
                    "block_type": "heading"
                }
            ]

        return semantic_search(query, top_k)

    client.semantic_search = Mock(side_effect=semantic_search)
    client.hierarchical_search = Mock(side_effect=hierarchical_search)

    return client


@pytest.fixture
def fastapi_app(db_session):
    """Create FastAPI app with search router"""
    app = FastAPI()
    app.include_router(router)

    # Override dependencies
    app.dependency_overrides[get_db] = lambda: db_session

    return app


@pytest.fixture
def api_client(fastapi_app):
    """Create FastAPI test client"""
    return TestClient(fastapi_app)


# ============================================================================
# Integration Tests - Complete Retrieval Pipeline
# ============================================================================

class TestSemanticSearchIntegration:
    """Integration tests for workspace-scoped semantic search."""

    def test_semantic_search_workspace_write_access(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Workspace write members can retrieve workspace files."""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Alice has workspace write access to both machine-learning files.
        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10,
            use_hierarchical=False
        )

        # Alice should see both files in the authorized workspace.
        assert len(results) >= 2
        uris = [r["uri"] for r in results]
        assert "viking://bucket/ml_intro.pdf" in uris
        assert "viking://bucket/deep_learning.pdf" in uris

        # Verify file_id is populated
        for result in results:
            assert "file_id" in result
            assert result["file_id"] > 0

    def test_semantic_search_workspace_read_access(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Workspace read members see the same workspace candidates."""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Bob has read access to the machine-learning workspace.
        results = service.search(
            query="machine learning",
            user_id=test_users["bob"].id,
            top_k=10,
            use_hierarchical=False
        )

        uris = [r["uri"] for r in results]
        assert "viking://bucket/ml_intro.pdf" in uris
        assert "viking://bucket/deep_learning.pdf" in uris

    def test_semantic_search_team_only_has_no_access(
        self, db_session, test_users, test_files, test_team, mock_agfs_client
    ):
        """Team membership does not grant retrieval access."""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Charlie is a team member but not a database workspace member.
        results = service.search(
            query="database",
            user_id=test_users["charlie"].id,
            top_k=10,
            use_hierarchical=False
        )

        uris = [r["uri"] for r in results]
        assert "viking://bucket/databases.pdf" not in uris

    def test_semantic_search_no_access(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test semantic search filters out inaccessible files"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Diana can read only the private workspace, not the security workspace.
        results = service.search(
            query="security",
            user_id=test_users["diana"].id,
            top_k=10,
            use_hierarchical=False
        )

        # Diana should only see private.pdf, not security.pdf
        uris = [r["uri"] for r in results]
        assert "viking://bucket/private.pdf" in uris
        assert "viking://bucket/security.pdf" not in uris

    def test_semantic_search_empty_results(
        self, db_session, test_users, test_files, mock_agfs_client
    ):
        """Test semantic search with no matching results"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Search for non-existent topic
        results = service.search(
            query="quantum physics",
            user_id=test_users["alice"].id,
            top_k=10,
            use_hierarchical=False
        )

        assert len(results) == 0


class TestHierarchicalSearchIntegration:
    """Integration tests for workspace-scoped hierarchical search."""

    def test_hierarchical_search_structure(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test hierarchical search considers document structure"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Alice searches for introduction using hierarchical search
        results = service.search(
            query="introduction",
            user_id=test_users["alice"].id,
            top_k=10,
            use_hierarchical=True
        )

        # Should return title and heading blocks
        assert len(results) > 0
        assert any(r["block_type"] == "title" for r in results)
        assert any(r["level"] == 1 for r in results)

        # Verify hierarchical search was called
        mock_agfs_client.hierarchical_search.assert_called()

    def test_hierarchical_search_with_workspace_read(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Hierarchical search respects workspace read scope."""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Bob searches with hierarchical search
        results = service.search(
            query="introduction",
            user_id=test_users["bob"].id,
            top_k=10,
            use_hierarchical=True
        )

        # Bob should only see files he has access to
        uris = [r["uri"] for r in results]
        for uri in uris:
            assert uri in [
                "viking://bucket/ml_intro.pdf",
                "viking://bucket/deep_learning.pdf",
            ]


class TestWorkspaceFilteringIntegration:
    """Integration tests for workspace filtering across scenarios."""

    def test_workspace_write_member_sees_workspace_files(
        self, db_session, test_users, test_files, mock_agfs_client
    ):
        """Workspace write membership, not owner_id, grants access."""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Alice has write access to the machine-learning workspace.
        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        file_ids = [r["file_id"] for r in results if "file_id" in r]
        assert test_files["ml_intro"].id in file_ids
        assert test_files["deep_learning"].id in file_ids

    def test_workspace_read_member_access(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Workspace read membership grants access to all workspace files."""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="machine learning",
            user_id=test_users["bob"].id,
            top_k=10
        )

        uris = [r["uri"] for r in results]
        assert "viking://bucket/ml_intro.pdf" in uris
        assert "viking://bucket/deep_learning.pdf" in uris

    def test_team_member_without_workspace_access_is_denied(
        self, db_session, test_users, test_files, test_team, mock_agfs_client
    ):
        """Team membership alone cannot expand retrieval scope."""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Charlie is a team member but has no database workspace membership.
        results = service.search(
            query="database",
            user_id=test_users["charlie"].id,
            top_k=10
        )

        uris = [r["uri"] for r in results]
        assert "viking://bucket/databases.pdf" not in uris

    def test_non_member_no_access(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test non-team member cannot access team files"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Diana is not in Engineering team
        results = service.search(
            query="database",
            user_id=test_users["diana"].id,
            top_k=10
        )

        # Diana should not see databases.pdf
        uris = [r["uri"] for r in results]
        assert "viking://bucket/databases.pdf" not in uris

    def test_cross_user_isolation(
        self, db_session, test_users, test_files, mock_agfs_client
    ):
        """Test users cannot see each other's private files"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Diana searches for security
        results = service.search(
            query="security",
            user_id=test_users["diana"].id,
            top_k=10
        )

        # Diana should not see charlie's security.pdf
        uris = [r["uri"] for r in results]
        assert "viking://bucket/security.pdf" not in uris


class TestRerankerIntegration:
    """Integration tests for reranking effectiveness"""

    def test_reranking_changes_order(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test reranking changes result order"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Get results without reranking
        results_no_rerank = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        # Apply reranking
        reranker = Reranker()
        results_reranked = reranker.rerank(
            query="machine learning",
            results=results_no_rerank,
            top_k=10
        )

        # Verify reranked_score is added
        assert all("reranked_score" in r for r in results_reranked)

        # Verify results are sorted by reranked_score
        scores = [r["reranked_score"] for r in results_reranked]
        assert scores == sorted(scores, reverse=True)

    def test_reranking_boosts_titles(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test reranking boosts title blocks"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Get hierarchical results (includes titles)
        results = service.search(
            query="introduction",
            user_id=test_users["alice"].id,
            top_k=10,
            use_hierarchical=True
        )

        # Apply reranking
        reranker = Reranker(hierarchical_boost=0.2)
        results_reranked = reranker.rerank(
            query="introduction",
            results=results,
            top_k=10
        )

        # Title blocks should get boost
        if len(results_reranked) > 0:
            # Find title block
            title_results = [r for r in results_reranked if r.get("block_type") == "title"]
            if title_results:
                # Title should have higher reranked_score than original
                title = title_results[0]
                assert title["reranked_score"] > title["score"]

    def test_reranking_boosts_early_pages(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test reranking boosts results from early pages"""
        # Create mock results with different pages
        mock_results = [
            {
                "text": "Content on page 10",
                "score": 0.80,
                "uri": "viking://bucket/ml_intro.pdf",
                "page": 10,
                "offset": 0,
                "bbox": None,
                "level": 0,
                "block_type": "text"
            },
            {
                "text": "Content on page 1",
                "score": 0.80,
                "uri": "viking://bucket/ml_intro.pdf",
                "page": 1,
                "offset": 0,
                "bbox": None,
                "level": 0,
                "block_type": "text"
            }
        ]

        reranker = Reranker(position_boost=0.1)
        results_reranked = reranker.rerank(
            query="content",
            results=mock_results,
            top_k=10
        )

        # Page 1 should rank higher than page 10 (with same base score)
        page_1_result = [r for r in results_reranked if r["page"] == 1][0]
        page_10_result = [r for r in results_reranked if r["page"] == 10][0]

        assert page_1_result["reranked_score"] > page_10_result["reranked_score"]

    def test_reranking_preserves_metadata(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test reranking preserves all metadata fields"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        reranker = Reranker()
        results_reranked = reranker.rerank(
            query="machine learning",
            results=results,
            top_k=10
        )

        # Verify all metadata is preserved
        for original, reranked in zip(results, results_reranked):
            assert reranked["text"] == original["text"]
            assert reranked["uri"] == original["uri"]
            assert reranked["page"] == original["page"]
            assert reranked["offset"] == original["offset"]
            assert reranked["bbox"] == original["bbox"]
            assert reranked["level"] == original["level"]
            assert reranked["block_type"] == original["block_type"]




class TestSearchAPIIntegration:
    """Integration tests for Search API endpoints"""

    def test_semantic_search_api_with_permissions(
        self, db_session, test_users, test_files, test_workspaces
    ):
        """Test semantic search API endpoint with workspace filtering."""
        # Create fresh app for this test
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: test_users["alice"].id

        client = TestClient(app)

        # Patch RetrievalService to use mock
        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_service = Mock()
            mock_service.search.return_value = [
                {
                    "text": "Machine learning is a subset of AI",
                    "score": 0.95,
                    "file_id": test_files["ml_intro"].id,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": [100.0, 200.0, 400.0, 250.0],
                    "level": 1,
                    "block_type": "heading"
                }
            ]
            MockRetrieval.return_value = mock_service

            # Make API request
            response = client.post(
                "/search/semantic",
                json={
                    "query": "machine learning",
                    "top_k": 10,
                    "use_rerank": False
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert "results" in data
            assert "total" in data
            assert "query_time_ms" in data
            assert len(data["results"]) == 1
            assert data["results"][0]["text"] == "Machine learning is a subset of AI"

    def test_semantic_search_api_with_reranking(
        self, db_session, test_users, test_files, test_workspaces
    ):
        """Test semantic search API with reranking enabled"""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: test_users["alice"].id

        client = TestClient(app)

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval, \
             patch("openrag.api.search_api.Reranker") as MockReranker:

            # Mock retrieval results
            mock_service = Mock()
            mock_service.search.return_value = [
                {
                    "text": "Result 1",
                    "score": 0.80,
                    "file_id": test_files["ml_intro"].id,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                },
                {
                    "text": "Result 2",
                    "score": 0.75,
                    "file_id": test_files["deep_learning"].id,
                    "uri": "viking://bucket/deep_learning.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                }
            ]
            MockRetrieval.return_value = mock_service

            # Mock reranker
            mock_reranker = Mock()
            mock_reranker.rerank.return_value = [
                {
                    "text": "Result 2",
                    "score": 0.75,
                    "reranked_score": 0.90,
                    "file_id": test_files["deep_learning"].id,
                    "uri": "viking://bucket/deep_learning.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                },
                {
                    "text": "Result 1",
                    "score": 0.80,
                    "reranked_score": 0.85,
                    "file_id": test_files["ml_intro"].id,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                }
            ]
            MockReranker.return_value = mock_reranker

            # Make API request with reranking
            response = client.post(
                "/search/semantic",
                json={
                    "query": "test query",
                    "top_k": 10,
                    "use_rerank": True
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data["results"]) == 2

            # Verify reranked scores are used
            assert data["results"][0]["score"] == 0.90  # reranked_score
            assert data["results"][1]["score"] == 0.85  # reranked_score

            # Verify reranker was called
            mock_reranker.rerank.assert_called_once()

    def test_hierarchical_search_api(
        self, db_session, test_users, test_files, test_workspaces
    ):
        """Test hierarchical search API endpoint"""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: test_users["alice"].id

        client = TestClient(app)

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_service = Mock()
            mock_service.search.return_value = [
                {
                    "text": "Chapter 1: Introduction",
                    "score": 0.95,
                    "file_id": test_files["ml_intro"].id,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": [50.0, 50.0, 550.0, 100.0],
                    "level": 1,
                    "block_type": "title"
                }
            ]
            MockRetrieval.return_value = mock_service

            # Make hierarchical search request
            response = client.post(
                "/search/hierarchical",
                json={
                    "query": "introduction",
                    "top_k": 5,
                    "use_rerank": False
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data["results"]) == 1
            assert data["results"][0]["block_type"] == "title"
            assert data["results"][0]["level"] == 1

            # Verify use_hierarchical=True was passed
            call_kwargs = mock_service.search.call_args[1]
            assert call_kwargs["use_hierarchical"] is True

    def test_api_different_users_different_results(
        self, db_session, test_users, test_files, test_workspaces
    ):
        """Test different users get different filtered results via API"""
        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_service = Mock()

            # Alice's results
            app_alice = FastAPI()
            app_alice.include_router(router)
            app_alice.dependency_overrides[get_db] = lambda: db_session
            app_alice.dependency_overrides[get_current_user] = lambda: test_users["alice"].id
            client_alice = TestClient(app_alice)

            mock_service.search.return_value = [
                {
                    "text": "Alice can see this",
                    "score": 0.9,
                    "file_id": test_files["ml_intro"].id,
                    "uri": "viking://bucket/ml_intro.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                }
            ]
            MockRetrieval.return_value = mock_service

            response_alice = client_alice.post(
                "/search/semantic",
                json={"query": "test", "top_k": 10, "use_rerank": False}
            )

            # Diana's results
            app_diana = FastAPI()
            app_diana.include_router(router)
            app_diana.dependency_overrides[get_db] = lambda: db_session
            app_diana.dependency_overrides[get_current_user] = lambda: test_users["diana"].id
            client_diana = TestClient(app_diana)

            mock_service.search.return_value = [
                {
                    "text": "Diana can see this",
                    "score": 0.8,
                    "file_id": test_files["private"].id,
                    "uri": "viking://bucket/private.pdf",
                    "page": 1,
                    "offset": 0,
                    "bbox": None,
                    "level": 0,
                    "block_type": "text"
                }
            ]

            response_diana = client_diana.post(
                "/search/semantic",
                json={"query": "test", "top_k": 10, "use_rerank": False}
            )

            # Verify both succeeded but with different results
            assert response_alice.status_code == 200
            assert response_diana.status_code == 200

            data_alice = response_alice.json()
            data_diana = response_diana.json()

            assert data_alice["results"][0]["file_id"] != data_diana["results"][0]["file_id"]

    def test_api_empty_results(self, db_session, test_users):
        """Test API returns empty results gracefully"""
        # Capture user ID before creating app to avoid lazy loading issues
        alice_id = test_users["alice"].id

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: alice_id

        client = TestClient(app)

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_service = Mock()
            mock_service.search.return_value = []
            MockRetrieval.return_value = mock_service

            response = client.post(
                "/search/semantic",
                json={"query": "nonexistent", "top_k": 10, "use_rerank": True}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["results"] == []
            assert data["total"] == 0

    def test_api_invalid_request(self):
        """Test API validates request parameters"""
        # Create minimal app just for validation testing
        app = FastAPI()
        app.include_router(router)

        # Mock dependencies to avoid database access
        app.dependency_overrides[get_db] = lambda: Mock()
        app.dependency_overrides[get_current_user] = lambda: 1

        client = TestClient(app)

        # Empty query - should fail validation before hitting database
        response = client.post(
            "/search/semantic",
            json={"query": "", "top_k": 10, "use_rerank": True}
        )
        assert response.status_code == 422

        # Invalid top_k - should fail validation
        response = client.post(
            "/search/semantic",
            json={"query": "test", "top_k": -1, "use_rerank": True}
        )
        assert response.status_code == 422

        # top_k too large - should fail validation
        response = client.post(
            "/search/semantic",
            json={"query": "test", "top_k": 1000, "use_rerank": True}
        )
        assert response.status_code == 422



class TestPositionPreservation:
    """Integration tests for position metadata preservation"""

    def test_position_metadata_preserved_through_pipeline(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test position metadata (page, offset, bbox, level) preserved through pipeline"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Get results
        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        # Apply reranking
        reranker = Reranker()
        results_reranked = reranker.rerank(
            query="machine learning",
            results=results,
            top_k=10
        )

        # Verify all position metadata is preserved
        for result in results_reranked:
            assert "page" in result
            assert "offset" in result
            assert "bbox" in result  # Can be None
            assert "level" in result
            assert isinstance(result["page"], int)
            assert isinstance(result["offset"], int)
            assert isinstance(result["level"], int)

    def test_bbox_format_preserved(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test bounding box format is preserved correctly"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        for result in results:
            bbox = result.get("bbox")
            if bbox is not None:
                # Should be list of 4 floats
                assert isinstance(bbox, list)
                assert len(bbox) == 4
                assert all(isinstance(x, (int, float)) for x in bbox)


class TestRetrievalAccuracy:
    """Integration tests for retrieval accuracy and relevance"""

    def test_relevant_results_for_ml_query(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test machine learning query returns ML-related documents"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        # Should return ML-related files
        assert len(results) > 0

        # Top results should be from ML files
        top_uris = [r["uri"] for r in results[:2]]
        assert any("ml_intro" in uri or "deep_learning" in uri for uri in top_uris)

    def test_relevant_results_for_database_query(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test database query returns database-related documents"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="database SQL",
            user_id=test_users["bob"].id,
            top_k=10
        )

        # Should return database-related files
        assert len(results) > 0

        # Top result should be from databases file
        assert "databases" in results[0]["uri"]

    def test_score_ordering(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test results are ordered by relevance score"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        # Scores should be in descending order
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_reranking_improves_relevance(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test reranking improves result relevance"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Get results
        results = service.search(
            query="machine learning introduction",
            user_id=test_users["alice"].id,
            top_k=10,
            use_hierarchical=True
        )

        if len(results) == 0:
            pytest.skip("No results to rerank")

        # Apply reranking
        reranker = Reranker(hierarchical_boost=0.2)
        results_reranked = reranker.rerank(
            query="machine learning introduction",
            results=results,
            top_k=10
        )

        # Title/heading blocks should rank higher after reranking
        # Find highest-level block
        high_level_blocks = [r for r in results_reranked if r.get("level", 0) >= 1]
        if high_level_blocks:
            # At least one high-level block should be in top 3
            top_3_levels = [r.get("level", 0) for r in results_reranked[:3]]
            assert any(level >= 1 for level in top_3_levels)


class TestEdgeCases:
    """Integration tests for edge cases and error scenarios"""

    def test_user_with_no_files(
        self, db_session, mock_agfs_client
    ):
        """Test search for user with no accessible files"""
        # Create user with no files
        new_user = User(
            username="newuser",
            email="newuser@example.com",
            password_hash="hash",
            full_name="New User"
        )
        db_session.add(new_user)
        db_session.commit()

        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="machine learning",
            user_id=new_user.id,
            top_k=10
        )

        # Should return empty results (all filtered out)
        assert len(results) == 0

    def test_search_with_top_k_limit(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test search respects top_k limit"""
        # Mock AGFS to return more results than top_k
        mock_agfs_client.semantic_search.return_value = [
            {
                "text": f"Result {i}",
                "score": 0.9 - (i * 0.1),
                "uri": "viking://bucket/ml_intro.pdf",
                "page": 1,
                "offset": i * 100,
                "bbox": None,
                "level": 0,
                "block_type": "text"
            }
            for i in range(5)
        ]

        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Search with small top_k
        results = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=1
        )

        # AGFS client should be called with top_k=1
        mock_agfs_client.semantic_search.assert_called_with("machine learning", top_k=1)

    def test_search_with_none_bbox(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test search handles None bbox values correctly"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        results = service.search(
            query="database",
            user_id=test_users["bob"].id,
            top_k=10
        )

        # Some results may have None bbox
        for result in results:
            bbox = result.get("bbox")
            # Should be either None or valid list
            assert bbox is None or (isinstance(bbox, list) and len(bbox) == 4)

    def test_concurrent_searches_different_users(
        self, db_session, test_users, test_files, test_workspaces, mock_agfs_client
    ):
        """Test concurrent searches by different users are isolated"""
        service = RetrievalService(db_session, agfs_client=mock_agfs_client)

        # Simulate concurrent searches
        results_alice = service.search(
            query="machine learning",
            user_id=test_users["alice"].id,
            top_k=10
        )

        results_bob = service.search(
            query="machine learning",
            user_id=test_users["bob"].id,
            top_k=10
        )

        # Results should differ because the users have different workspace scopes.
        alice_uris = set(r["uri"] for r in results_alice)
        bob_uris = set(r["uri"] for r in results_bob)

        # Alice should see more files than Bob
        assert len(alice_uris) >= len(bob_uris)

        # Bob should not see deep_learning (not shared)
        assert "viking://bucket/deep_learning.pdf" not in bob_uris

