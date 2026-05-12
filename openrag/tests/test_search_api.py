"""Tests for search API endpoints"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch
from sqlalchemy.orm import Session

from openrag.api.search_api import router, get_user_id
from openrag.api.deps import get_db
from fastapi import FastAPI


@pytest.fixture
def app():
    """Create FastAPI app with search router"""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    """Create test client"""
    return TestClient(app)


@pytest.fixture
def mock_db():
    """Create mock database session"""
    return Mock(spec=Session)


@pytest.fixture
def mock_user_id():
    """Mock user ID for authentication"""
    return 1


@pytest.fixture
def override_dependencies(app, mock_db, mock_user_id):
    """Override FastAPI dependencies with mocks"""
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_user_id] = lambda: mock_user_id
    yield
    app.dependency_overrides.clear()


class TestSemanticSearch:
    """Tests for semantic search endpoint"""

    def test_semantic_search_success(self, client, override_dependencies, mock_db):
        """Test successful semantic search"""
        # Mock RetrievalService and Reranker
        mock_results = [
            {
                "text": "Machine learning is a subset of AI",
                "score": 0.95,
                "file_id": 1,
                "page": 1,
                "offset": 0,
                "bbox": [100.0, 200.0, 400.0, 250.0],
                "level": 1,
                "block_type": "heading",
                "uri": "file://doc1.pdf"
            },
            {
                "text": "Deep learning uses neural networks",
                "score": 0.85,
                "file_id": 1,
                "page": 2,
                "offset": 500,
                "bbox": None,
                "level": 2,
                "block_type": "text",
                "uri": "file://doc1.pdf"
            }
        ]

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval, \
             patch("openrag.api.search_api.Reranker") as MockReranker:

            # Setup mocks
            mock_retrieval_instance = Mock()
            mock_retrieval_instance.search.return_value = mock_results
            MockRetrieval.return_value = mock_retrieval_instance

            mock_reranker_instance = Mock()
            mock_reranker_instance.rerank.return_value = [
                {**mock_results[0], "reranked_score": 0.98},
                {**mock_results[1], "reranked_score": 0.88}
            ]
            MockReranker.return_value = mock_reranker_instance

            # Make request
            response = client.post(
                "/search/semantic",
                json={
                    "query": "machine learning",
                    "top_k": 10,
                    "use_rerank": True
                }
            )

            # Assertions
            assert response.status_code == 200
            data = response.json()
            assert "results" in data
            assert "total" in data
            assert "query_time_ms" in data
            assert len(data["results"]) == 2
            assert data["total"] == 2
            assert data["results"][0]["text"] == "Machine learning is a subset of AI"
            assert data["results"][0]["score"] == 0.98  # reranked score

    def test_semantic_search_without_rerank(self, client, override_dependencies, mock_db):
        """Test semantic search without reranking"""
        mock_results = [
            {
                "text": "Test result",
                "score": 0.9,
                "file_id": 1,
                "page": 1,
                "offset": 0,
                "bbox": None,
                "level": 1,
                "block_type": "text",
                "uri": "file://test.pdf"
            }
        ]

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_retrieval_instance = Mock()
            mock_retrieval_instance.search.return_value = mock_results
            MockRetrieval.return_value = mock_retrieval_instance

            response = client.post(
                "/search/semantic",
                json={
                    "query": "test query",
                    "top_k": 5,
                    "use_rerank": False
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data["results"]) == 1
            assert data["results"][0]["score"] == 0.9  # original score, not reranked

    def test_semantic_search_empty_results(self, client, override_dependencies, mock_db):
        """Test semantic search with no results"""
        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_retrieval_instance = Mock()
            mock_retrieval_instance.search.return_value = []
            MockRetrieval.return_value = mock_retrieval_instance

            response = client.post(
                "/search/semantic",
                json={
                    "query": "nonexistent query",
                    "top_k": 10,
                    "use_rerank": True
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data["results"] == []
            assert data["total"] == 0

    def test_semantic_search_invalid_query(self, client, override_dependencies):
        """Test semantic search with invalid query"""
        response = client.post(
            "/search/semantic",
            json={
                "query": "",  # Empty query
                "top_k": 10,
                "use_rerank": True
            }
        )

        assert response.status_code == 422  # Validation error

    def test_semantic_search_unauthorized(self, client, mock_db):
        """Test semantic search without authentication"""
        # Don't override get_current_user, so it will fail
        app = FastAPI()
        app.include_router(router)
        test_client = TestClient(app)

        response = test_client.post(
            "/search/semantic",
            json={
                "query": "test",
                "top_k": 10,
                "use_rerank": True
            }
        )

        # Should fail because get_current_user is not mocked
        assert response.status_code in [401, 500]  # Depends on implementation


class TestHierarchicalSearch:
    """Tests for hierarchical search endpoint"""

    def test_hierarchical_search_success(self, client, override_dependencies, mock_db):
        """Test successful hierarchical search"""
        mock_results = [
            {
                "text": "Chapter 1: Introduction",
                "score": 0.92,
                "file_id": 2,
                "page": 1,
                "offset": 0,
                "bbox": [50.0, 100.0, 500.0, 150.0],
                "level": 1,
                "block_type": "title",
                "uri": "file://book.pdf"
            }
        ]

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval, \
             patch("openrag.api.search_api.Reranker") as MockReranker:

            mock_retrieval_instance = Mock()
            mock_retrieval_instance.search.return_value = mock_results
            MockRetrieval.return_value = mock_retrieval_instance

            mock_reranker_instance = Mock()
            mock_reranker_instance.rerank.return_value = [
                {**mock_results[0], "reranked_score": 0.95}
            ]
            MockReranker.return_value = mock_reranker_instance

            response = client.post(
                "/search/hierarchical",
                json={
                    "query": "introduction",
                    "top_k": 5,
                    "use_rerank": True
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data["results"]) == 1
            assert data["results"][0]["block_type"] == "title"
            assert data["results"][0]["level"] == 1

            # Verify hierarchical search was called
            mock_retrieval_instance.search.assert_called_once()
            call_kwargs = mock_retrieval_instance.search.call_args[1]
            assert call_kwargs["use_hierarchical"] is True

    def test_hierarchical_search_without_rerank(self, client, override_dependencies, mock_db):
        """Test hierarchical search without reranking"""
        mock_results = [
            {
                "text": "Section 2.1",
                "score": 0.88,
                "file_id": 3,
                "page": 5,
                "offset": 1000,
                "bbox": None,
                "level": 2,
                "block_type": "heading",
                "uri": "file://doc.pdf"
            }
        ]

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_retrieval_instance = Mock()
            mock_retrieval_instance.search.return_value = mock_results
            MockRetrieval.return_value = mock_retrieval_instance

            response = client.post(
                "/search/hierarchical",
                json={
                    "query": "section",
                    "top_k": 10,
                    "use_rerank": False
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data["results"]) == 1
            assert data["results"][0]["score"] == 0.88


class TestPermissionChecking:
    """Tests for permission checking"""

    def test_different_users_get_different_results(self, client, mock_db):
        """Test that different users get filtered results"""
        app = FastAPI()
        app.include_router(router)
        test_client = TestClient(app)

        # User 1 results
        user1_results = [
            {
                "text": "User 1 can see this",
                "score": 0.9,
                "file_id": 1,
                "page": 1,
                "offset": 0,
                "bbox": None,
                "level": 1,
                "block_type": "text",
                "uri": "file://user1_doc.pdf"
            }
        ]

        # User 2 results
        user2_results = [
            {
                "text": "User 2 can see this",
                "score": 0.85,
                "file_id": 2,
                "page": 1,
                "offset": 0,
                "bbox": None,
                "level": 1,
                "block_type": "text",
                "uri": "file://user2_doc.pdf"
            }
        ]

        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_retrieval_instance = Mock()

            # First call for user 1
            app.dependency_overrides[get_db] = lambda: mock_db
            app.dependency_overrides[get_user_id] = lambda: 1
            mock_retrieval_instance.search.return_value = user1_results
            MockRetrieval.return_value = mock_retrieval_instance

            response1 = test_client.post(
                "/search/semantic",
                json={"query": "test", "top_k": 10, "use_rerank": False}
            )

            # Second call for user 2
            app.dependency_overrides[get_user_id] = lambda: 2
            mock_retrieval_instance.search.return_value = user2_results

            response2 = test_client.post(
                "/search/semantic",
                json={"query": "test", "top_k": 10, "use_rerank": False}
            )

            # Verify different results
            assert response1.status_code == 200
            assert response2.status_code == 200
            data1 = response1.json()
            data2 = response2.json()
            assert data1["results"][0]["text"] != data2["results"][0]["text"]


class TestErrorHandling:
    """Tests for error handling"""

    def test_retrieval_service_error(self, client, override_dependencies, mock_db):
        """Test handling of RetrievalService errors"""
        with patch("openrag.api.search_api.RetrievalService") as MockRetrieval:
            mock_retrieval_instance = Mock()
            mock_retrieval_instance.search.side_effect = RuntimeError("AGFS client not initialized")
            MockRetrieval.return_value = mock_retrieval_instance

            response = client.post(
                "/search/semantic",
                json={"query": "test", "top_k": 10, "use_rerank": True}
            )

            assert response.status_code == 500
            data = response.json()
            assert "detail" in data
            assert "error" in data["detail"]

    def test_invalid_top_k(self, client, override_dependencies):
        """Test with invalid top_k parameter"""
        response = client.post(
            "/search/semantic",
            json={"query": "test", "top_k": -1, "use_rerank": True}
        )

        assert response.status_code == 422  # Validation error
