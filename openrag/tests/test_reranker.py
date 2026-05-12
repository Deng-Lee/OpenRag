"""Tests for reranker with hierarchical and position boosting"""

import pytest
from unittest.mock import Mock, patch

from openrag.retrieval.reranker import Reranker


class TestReranker:
    """Test Reranker class"""

    def test_init_default_params(self):
        """Test initialization with default parameters"""
        reranker = Reranker()
        assert reranker.model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"
        assert reranker.hierarchical_boost == 0.1
        assert reranker.position_boost == 0.05
        assert reranker.model is None  # Mock model not loaded yet

    def test_init_custom_params(self):
        """Test initialization with custom parameters"""
        reranker = Reranker(
            model_name="custom-model",
            hierarchical_boost=0.2,
            position_boost=0.1
        )
        assert reranker.model_name == "custom-model"
        assert reranker.hierarchical_boost == 0.2
        assert reranker.position_boost == 0.1

    def test_rerank_empty_results(self):
        """Test reranking with empty results"""
        reranker = Reranker()
        results = reranker.rerank("test query", [], top_k=10)
        assert results == []

    def test_rerank_basic(self):
        """Test basic reranking without hierarchical/position info"""
        reranker = Reranker()

        results = [
            {"text": "Python is a programming language", "score": 0.5, "file_id": 1},
            {"text": "Java is also a programming language", "score": 0.6, "file_id": 2},
            {"text": "JavaScript for web development", "score": 0.4, "file_id": 3}
        ]

        reranked = reranker.rerank("programming language", results, top_k=3)

        assert len(reranked) == 3
        # Results should have reranked_score
        assert all("reranked_score" in r for r in reranked)
        # Should be sorted by reranked_score descending
        assert reranked[0]["reranked_score"] >= reranked[1]["reranked_score"]
        assert reranked[1]["reranked_score"] >= reranked[2]["reranked_score"]

    def test_rerank_with_top_k_limit(self):
        """Test reranking with top_k limit"""
        reranker = Reranker()

        results = [
            {"text": f"Document {i}", "score": 0.5 + i * 0.01, "file_id": i}
            for i in range(20)
        ]

        reranked = reranker.rerank("test query", results, top_k=5)

        assert len(reranked) == 5

    def test_rerank_with_hierarchical_boost(self):
        """Test reranking with hierarchical information"""
        reranker = Reranker(hierarchical_boost=0.2)

        results = [
            {"text": "Regular paragraph query", "score": 0.7, "file_id": 1, "level": 0, "block_type": "text"},
            {"text": "Main heading query", "score": 0.6, "file_id": 2, "level": 1, "block_type": "heading"},
            {"text": "Subheading query", "score": 0.65, "file_id": 3, "level": 2, "block_type": "heading"},
            {"text": "Title query", "score": 0.5, "file_id": 4, "level": 0, "block_type": "title"}
        ]

        reranked = reranker.rerank("test query", results, top_k=4)

        # Title and headings should get boosted
        # Check that hierarchical boost was applied
        assert all("reranked_score" in r for r in reranked)

        # Find the title result
        title_result = next(r for r in reranked if r.get("block_type") == "title")
        # Title should have received boost (base score + hierarchical boost)
        # With keyword overlap, base score should be > 0, plus 0.2 boost for title
        assert title_result["reranked_score"] > title_result["score"] * 0.4

    def test_rerank_with_position_boost(self):
        """Test reranking with position information"""
        reranker = Reranker(position_boost=0.1)

        results = [
            {"text": "Content query on page 5", "score": 0.7, "file_id": 1, "page": 5, "bbox": [0, 0, 100, 50]},
            {"text": "Content query on page 1", "score": 0.65, "file_id": 2, "page": 1, "bbox": [0, 0, 200, 100]},
            {"text": "Content query on page 10", "score": 0.68, "file_id": 3, "page": 10, "bbox": [0, 0, 50, 25]}
        ]

        reranked = reranker.rerank("test query", results, top_k=3)

        # Earlier pages and larger bboxes should get boosted
        assert all("reranked_score" in r for r in reranked)

        # Page 1 result should get boost compared to page 10
        page1_result = next(r for r in reranked if r.get("page") == 1)
        page10_result = next(r for r in reranked if r.get("page") == 10)
        # Page 1 should rank higher than page 10 due to position boost
        assert page1_result["reranked_score"] > page10_result["reranked_score"]

    def test_rerank_combined_boosts(self):
        """Test reranking with both hierarchical and position boosts"""
        reranker = Reranker(hierarchical_boost=0.15, position_boost=0.05)

        results = [
            {
                "text": "Regular text on page 10",
                "score": 0.8,
                "file_id": 1,
                "page": 10,
                "level": 0,
                "block_type": "text",
                "bbox": [0, 0, 100, 50]
            },
            {
                "text": "Heading on page 1",
                "score": 0.6,
                "file_id": 2,
                "page": 1,
                "level": 1,
                "block_type": "heading",
                "bbox": [0, 0, 200, 100]
            }
        ]

        reranked = reranker.rerank("test query", results, top_k=2)

        # Heading on page 1 should get both boosts and potentially rank higher
        assert len(reranked) == 2
        assert all("reranked_score" in r for r in reranked)

    def test_compute_cross_encoder_score_mock(self):
        """Test cross-encoder score computation with mock"""
        reranker = Reranker()

        # Mock scoring should return normalized score based on text similarity
        score = reranker._compute_cross_encoder_score("python programming", "Python is great")
        assert 0.0 <= score <= 1.0

    def test_apply_hierarchical_boost(self):
        """Test hierarchical boost calculation"""
        reranker = Reranker(hierarchical_boost=0.2)

        # Test title boost
        boosted = reranker._apply_hierarchical_boost(0.5, level=0, block_type="title")
        assert boosted > 0.5

        # Test heading level 1 boost
        boosted = reranker._apply_hierarchical_boost(0.5, level=1, block_type="heading")
        assert boosted > 0.5

        # Test heading level 2 boost (smaller)
        boosted_l2 = reranker._apply_hierarchical_boost(0.5, level=2, block_type="heading")
        assert boosted_l2 > 0.5

        # Level 1 should get more boost than level 2
        boosted_l1 = reranker._apply_hierarchical_boost(0.5, level=1, block_type="heading")
        assert boosted_l1 > boosted_l2

        # Test no boost for regular text
        no_boost = reranker._apply_hierarchical_boost(0.5, level=0, block_type="text")
        assert no_boost == 0.5

    def test_apply_hierarchical_boost_missing_info(self):
        """Test hierarchical boost with missing information"""
        reranker = Reranker(hierarchical_boost=0.2)

        # No boost if level/block_type missing
        score = reranker._apply_hierarchical_boost(0.5, level=None, block_type=None)
        assert score == 0.5

    def test_apply_position_boost(self):
        """Test position boost calculation"""
        reranker = Reranker(position_boost=0.1)

        # Test early page boost
        boosted_p1 = reranker._apply_position_boost(0.5, bbox=[0, 0, 200, 100], page=1)
        assert boosted_p1 > 0.5

        # Test later page (less boost)
        boosted_p10 = reranker._apply_position_boost(0.5, bbox=[0, 0, 200, 100], page=10)
        assert boosted_p10 > 0.5

        # Earlier page should get more boost
        assert boosted_p1 > boosted_p10

        # Test larger bbox boost
        boosted_large = reranker._apply_position_boost(0.5, bbox=[0, 0, 300, 200], page=5)
        boosted_small = reranker._apply_position_boost(0.5, bbox=[0, 0, 50, 25], page=5)
        assert boosted_large > boosted_small

    def test_apply_position_boost_missing_info(self):
        """Test position boost with missing information"""
        reranker = Reranker(position_boost=0.1)

        # No boost if bbox/page missing
        score = reranker._apply_position_boost(0.5, bbox=None, page=None)
        assert score == 0.5

        # Partial boost with only page
        score = reranker._apply_position_boost(0.5, bbox=None, page=1)
        assert score > 0.5

    def test_expand_context(self):
        """Test context expansion with parent chunk"""
        from unittest.mock import MagicMock

        reranker = Reranker()

        # Mock database session
        mock_db = MagicMock()

        # Mock parent chunk
        mock_parent = MagicMock()
        mock_parent.text = "This is the parent context"
        mock_parent.level = 1
        mock_parent.block_type = "heading"

        mock_db.execute.return_value.scalar_one_or_none.return_value = mock_parent

        result = {
            "text": "Child chunk text",
            "score": 0.8,
            "file_id": 1,
            "parent_chunk_id": 123
        }

        expanded = reranker.expand_context(result, mock_db)

        assert "parent_context" in expanded
        assert expanded["parent_context"]["text"] == "This is the parent context"
        assert expanded["parent_context"]["level"] == 1
        assert expanded["parent_context"]["block_type"] == "heading"

    def test_expand_context_no_parent(self):
        """Test context expansion when no parent exists"""
        from unittest.mock import MagicMock

        reranker = Reranker()

        # Mock database session returning None
        mock_db = MagicMock()
        mock_db.execute.return_value.scalar_one_or_none.return_value = None

        result = {
            "text": "Chunk without parent",
            "score": 0.8,
            "file_id": 1,
            "parent_chunk_id": None
        }

        expanded = reranker.expand_context(result, mock_db)

        assert "parent_context" in expanded
        assert expanded["parent_context"] is None

    def test_expand_context_missing_parent_id(self):
        """Test context expansion when parent_chunk_id is missing"""
        from unittest.mock import MagicMock

        reranker = Reranker()
        mock_db = MagicMock()

        result = {
            "text": "Chunk without parent_chunk_id field",
            "score": 0.8,
            "file_id": 1
        }

        expanded = reranker.expand_context(result, mock_db)

        assert "parent_context" in expanded
        assert expanded["parent_context"] is None

    def test_rerank_preserves_original_fields(self):
        """Test that reranking preserves all original fields"""
        reranker = Reranker()

        results = [
            {
                "text": "Test content",
                "score": 0.7,
                "file_id": 1,
                "page": 1,
                "offset": 100,
                "bbox": [0, 0, 100, 50],
                "level": 1,
                "uri": "viking://bucket/file.pdf",
                "custom_field": "custom_value"
            }
        ]

        reranked = reranker.rerank("test query", results, top_k=1)

        assert len(reranked) == 1
        result = reranked[0]

        # All original fields should be preserved
        assert result["text"] == "Test content"
        assert result["score"] == 0.7
        assert result["file_id"] == 1
        assert result["page"] == 1
        assert result["offset"] == 100
        assert result["bbox"] == [0, 0, 100, 50]
        assert result["level"] == 1
        assert result["uri"] == "viking://bucket/file.pdf"
        assert result["custom_field"] == "custom_value"

        # New field should be added
        assert "reranked_score" in result
