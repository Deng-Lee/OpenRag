"""Tests for hierarchy system (L0/L1/L2 generation and smart updates)."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from pathlib import Path
import tempfile
import os

from src.openrag.hierarchy.models import HierarchyResult, DirectoryHierarchy, Section
from src.openrag.hierarchy.document_hierarchy_builder import DocumentHierarchyBuilder
from src.openrag.hierarchy.directory_hierarchy_manager import DirectoryHierarchyManager
from src.openrag.hierarchy.hierarchy_storage import HierarchyStorage
from src.openrag.hierarchy.smart_update_engine import SmartUpdateEngine, cosine_similarity
from src.openrag.hierarchy.utils import extract_key_sentences, truncate_to_tokens, count_tokens
import numpy as np


class TestHierarchyModels:
    """Tests for hierarchy data models."""

    def test_section_creation(self):
        """Test Section dataclass."""
        section = Section(
            title="Introduction",
            level=1,
            content="This is the introduction section.",
            start_offset=0,
            end_offset=100
        )
        assert section.title == "Introduction"
        assert section.level == 1
        assert section.content == "This is the introduction section."
        assert section.start_offset == 0
        assert section.end_offset == 100

    def test_hierarchy_result_creation(self):
        """Test HierarchyResult dataclass."""
        result = HierarchyResult(
            l0="Document summary",
            l1="## S1\n\nContent 1",
            l2=["chunk1", "chunk2"],
        )
        assert result.l0 == "Document summary"
        assert "S1" in result.l1
        assert len(result.l2) == 2

    def test_directory_hierarchy_creation(self):
        """Test DirectoryHierarchy dataclass."""
        hierarchy = DirectoryHierarchy(
            l0="Directory summary",
            l1="## Contents\n- Doc1\n- Doc2"
        )
        assert hierarchy.l0 == "Directory summary"
        assert "Contents" in hierarchy.l1


class TestHierarchyUtils:
    """Tests for utility functions."""

    def test_extract_key_sentences(self):
        """Test key sentence extraction."""
        text = "First paragraph here. Second sentence.\n\nSecond paragraph starts. More text."
        sentences = extract_key_sentences(text, max_sentences=2)
        assert len(sentences) == 2
        assert "First paragraph" in sentences[0]
        assert "Second paragraph" in sentences[1]

    def test_extract_key_sentences_limited(self):
        """Test sentence extraction with limit."""
        text = "Para 1.\n\nPara 2.\n\nPara 3.\n\nPara 4."
        sentences = extract_key_sentences(text, max_sentences=2)
        assert len(sentences) == 2

    def test_count_tokens(self):
        """Test token counting approximation."""
        # Chinese text: ~1 token per 4 chars
        text = "这是一个中文测试文本"
        tokens = count_tokens(text)
        assert tokens == len(text) // 4

    def test_truncate_to_tokens(self):
        """Test text truncation."""
        text = "A" * 1000  # 1000 chars = ~250 tokens
        truncated = truncate_to_tokens(text, max_tokens=50)
        assert len(truncated) <= 200  # 50 tokens * 4 chars

    def test_truncate_to_tokens_preserves_sentence(self):
        """Test truncation at sentence boundary."""
        text = "This is a sentence. And another one here. Third sentence ends."
        truncated = truncate_to_tokens(text, max_tokens=10)
        # Should truncate at sentence boundary
        assert truncated.endswith('.')


class TestDocumentHierarchyBuilder:
    """Tests for document hierarchy builder."""

    @pytest.fixture
    def builder(self):
        """Create hierarchy builder."""
        return DocumentHierarchyBuilder(
            l0_max_tokens=100,
            l1_section_preview_tokens=50
        )

    @pytest.fixture
    def mock_chunks(self):
        """Create mock chunks."""
        from types import SimpleNamespace
        return [
            SimpleNamespace(text="# Introduction\nThis is the introduction.", level=1, start_offset=0, end_offset=50),
            SimpleNamespace(text="## Background\nSome background info.", level=2, start_offset=50, end_offset=100),
            SimpleNamespace(text="Regular content here.", level=0, start_offset=100, end_offset=150),
            SimpleNamespace(text="# Methods\nMethodology description.", level=1, start_offset=150, end_offset=200),
        ]

    def test_build_hierarchy(self, builder, mock_chunks):
        """Test complete hierarchy generation."""
        result = builder.build_hierarchy(mock_chunks)

        assert result.l0 is not None
        assert len(result.l0) > 0
        assert isinstance(result.l1, str)
        assert "Document overview" in result.l1
        assert result.l2 == mock_chunks

    def test_l0_derived_from_overview(self, builder, mock_chunks):
        """L0 abstract is derived from L1 overview text."""
        result = builder.build_hierarchy(mock_chunks)
        l0_direct = builder._abstract_from_overview(result.l1)
        assert l0_direct == result.l0

    def test_generate_l1_sections_from_chunks(self, builder, mock_chunks):
        """Structured sections helper (navigation building blocks)."""
        sections = builder._generate_l1_sections(mock_chunks)

        assert len(sections) > 0
        assert all(isinstance(s, Section) for s in sections)

    def test_build_hierarchy_empty_chunks(self, builder):
        """Test handling of empty chunks."""
        result = builder.build_hierarchy([])
        assert result.l0 == ""
        assert result.l1 == ""
        assert result.l2 == []


class TestDirectoryHierarchyManager:
    """Tests for directory hierarchy manager."""

    @pytest.fixture
    def manager(self):
        """Create directory manager."""
        return DirectoryHierarchyManager(l0_max_tokens=500)

    def test_aggregate_directory(self, manager):
        """Test directory aggregation."""
        children_l0s = [
            "Document 1 summary about topic A.",
            "Document 2 summary about topic B.",
            "Document 3 summary about topic C."
        ]
        children_names = ["doc1.pdf", "doc2.pdf", "doc3.pdf"]

        hierarchy = manager.aggregate_directory(
            directory_path="/workspace/docs",
            children_l0s=children_l0s,
            children_names=children_names
        )

        assert hierarchy.l0 is not None
        assert "3" in hierarchy.l0 or "文档" in hierarchy.l0  # Should mention count
        assert hierarchy.l1 is not None
        assert "doc1.pdf" in hierarchy.l1

    def test_generate_directory_l0(self, manager):
        """Test L0 generation for directory."""
        children_l0s = ["Summary 1", "Summary 2"]
        l0 = manager._generate_directory_l0("/workspace/docs", children_l0s)

        assert l0 is not None
        assert "docs" in l0 or "文档" in l0

    def test_generate_directory_l1(self, manager):
        """Test L1 generation for directory."""
        children_l0s = ["Doc 1 summary", "Doc 2 summary"]
        children_names = ["file1.txt", "file2.txt"]

        l1 = manager._generate_directory_l1(children_l0s, children_names)

        assert "file1.txt" in l1
        assert "file2.txt" in l1


class TestHierarchyStorage:
    """Tests for hierarchy storage."""

    @pytest.fixture
    def temp_storage(self):
        """Create temporary storage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = HierarchyStorage(base_path=tmpdir)
            yield storage

    @pytest.fixture
    def sample_hierarchy(self):
        """Create sample hierarchy."""
        return HierarchyResult(
            l0="Sample document summary",
            l1="## Section 1\n\nContent 1\n\n## Section 2\n\nContent 2\n",
            l2=[],
        )

    def test_save_and_load_document_hierarchy(self, temp_storage, sample_hierarchy):
        """Test document hierarchy persistence."""
        # Save
        temp_storage.save_document_hierarchy(
            file_uri="workspace/test.pdf",
            hierarchy=sample_hierarchy
        )

        # Load
        loaded = temp_storage.load_document_hierarchy(file_uri="workspace/test.pdf")

        assert loaded is not None
        assert loaded.l0 == sample_hierarchy.l0
        assert loaded.l1 == sample_hierarchy.l1

    def test_save_document_hierarchy_with_parameters(self, temp_storage):
        """Test saving with individual parameters."""
        temp_storage.save_document_hierarchy(
            file_uri="test/doc.txt",
            l0="Summary",
            l1=[{"title": "S1", "level": 1, "content": "C1"}],
            l2=[{"text": "chunk"}]
        )

        loaded = temp_storage.load_document_hierarchy(file_uri="test/doc.txt")
        assert loaded is not None

    def test_load_nonexistent_hierarchy(self, temp_storage):
        """Test loading non-existent hierarchy."""
        result = temp_storage.load_document_hierarchy(file_uri="nonexistent/file.pdf")
        assert result is None

    def test_load_l0(self, temp_storage, sample_hierarchy):
        """Test loading only L0 content."""
        temp_storage.save_document_hierarchy(
            file_uri="test/doc.pdf",
            hierarchy=sample_hierarchy
        )

        l0 = temp_storage.load_l0(file_uri="test/doc.pdf")
        assert l0 == sample_hierarchy.l0

    def test_get_path_methods(self, temp_storage):
        """Test path getter methods."""
        p0 = temp_storage.get_l0_path("test/doc.pdf").replace("\\", "/")
        p1 = temp_storage.get_l1_path("test/doc.pdf").replace("\\", "/")
        p2 = temp_storage.get_l2_path("test/doc.pdf").replace("\\", "/")
        assert p0.endswith(".abstract.md")
        assert p1.endswith(".overview.md")
        assert p2.rstrip("/").endswith("/chunks")

    def test_save_and_load_directory_hierarchy(self, temp_storage):
        """Test directory hierarchy persistence."""
        hierarchy = DirectoryHierarchy(
            l0="Directory summary",
            l1="## Contents\n- File 1"
        )

        temp_storage.save_directory_hierarchy(
            file_uri="workspace/docs",
            hierarchy=hierarchy
        )

        loaded = temp_storage.load_directory_hierarchy(file_uri="workspace/docs")
        assert loaded is not None
        assert loaded.l0 == hierarchy.l0
        assert loaded.l1 == hierarchy.l1


class TestSmartUpdateEngine:
    """Tests for smart update engine."""

    def test_cosine_similarity_identical(self):
        """Test similarity of identical vectors."""
        vec = np.array([1.0, 2.0, 3.0])
        similarity = cosine_similarity(vec, vec)
        assert abs(similarity - 1.0) < 0.001

    def test_cosine_similarity_orthogonal(self):
        """Test similarity of orthogonal vectors."""
        vec1 = np.array([1.0, 0.0])
        vec2 = np.array([0.0, 1.0])
        similarity = cosine_similarity(vec1, vec2)
        assert abs(similarity) < 0.001

    def test_cosine_similarity_opposite(self):
        """Test similarity of opposite vectors."""
        vec1 = np.array([1.0, 2.0, 3.0])
        vec2 = np.array([-1.0, -2.0, -3.0])
        similarity = cosine_similarity(vec1, vec2)
        assert abs(similarity + 1.0) < 0.001

    def test_cosine_similarity_zero_vector(self):
        """Test similarity with zero vector."""
        vec1 = np.array([1.0, 2.0, 3.0])
        vec2 = np.array([0.0, 0.0, 0.0])
        similarity = cosine_similarity(vec1, vec2)
        assert similarity == 0.0

    @pytest.fixture
    def engine(self):
        """Create smart update engine."""
        return SmartUpdateEngine(similarity_threshold=0.95)

    def test_get_parent_path(self, engine):
        """Test parent path extraction."""
        assert engine._get_parent_path("a/b/c.txt") == "a/b"
        assert engine._get_parent_path("a/b") == "a"
        assert engine._get_parent_path("a") is None
        assert engine._get_parent_path("a/b/c/d.txt") == "a/b/c"

    def test_should_update_parent_placeholder(self, engine):
        """Test update decision (placeholder always returns True)."""
        result = engine.should_update_parent("/parent", "child L0")
        # Placeholder implementation always returns True
        assert result is True

    def test_propagate_update_with_storage(self):
        """Test update propagation with storage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = HierarchyStorage(base_path=tmpdir)

            # Save parent L0
            storage.save_directory_hierarchy(
                file_uri="a/b",
                l0="Parent directory summary",
                l1="Contents"
            )

            engine = SmartUpdateEngine(
                similarity_threshold=0.95,
                hierarchy_storage=storage
            )

            # Propagate update
            paths = engine.propagate_update(
                file_path="a/b/c.txt",
                file_l0="New file L0 content"
            )

            # Should include parent path
            assert "a/b" in paths


class TestHierarchyIntegration:
    """Integration tests for hierarchy system."""

    @pytest.fixture
    def temp_storage(self):
        """Create temporary storage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield HierarchyStorage(base_path=tmpdir)

    def test_full_document_processing_flow(self, temp_storage):
        """Test complete document processing flow."""
        # Create mock chunks
        from types import SimpleNamespace
        chunks = [
            SimpleNamespace(
                text="# Introduction\nThis document describes the system.",
                level=1,
                start_offset=0,
                end_offset=50
            ),
            SimpleNamespace(
                text="## Overview\nThe system has multiple components.",
                level=2,
                start_offset=50,
                end_offset=100
            ),
            SimpleNamespace(
                text="Regular content paragraph.",
                level=0,
                start_offset=100,
                end_offset=150
            )
        ]

        # Build hierarchy
        builder = DocumentHierarchyBuilder()
        hierarchy = builder.build_hierarchy(chunks)

        # Save hierarchy
        temp_storage.save_document_hierarchy(
            file_uri="workspace/document.pdf",
            hierarchy=hierarchy
        )

        # Load and verify
        loaded = temp_storage.load_document_hierarchy(file_uri="workspace/document.pdf")

        assert loaded is not None
        assert loaded.l0 is not None
        assert isinstance(loaded.l1, str)
        assert len(loaded.l1) > 0

    def test_directory_aggregation_flow(self, temp_storage):
        """Test directory aggregation from children."""
        # Create and save multiple document hierarchies
        children = [
            ("docs/file1.pdf", "Summary of document 1 about topic A."),
            ("docs/file2.pdf", "Summary of document 2 about topic B."),
            ("docs/file3.pdf", "Summary of document 3 about topic C."),
        ]

        for uri, l0 in children:
            temp_storage.save_document_hierarchy(
                file_uri=uri,
                l0=l0,
                l1=[],
                l2=[]
            )

        # Aggregate directory
        manager = DirectoryHierarchyManager()
        children_l0s = [l0 for _, l0 in children]
        children_names = [uri.split('/')[-1] for uri, _ in children]

        dir_hierarchy = manager.aggregate_directory(
            directory_path="docs",
            children_l0s=children_l0s,
            children_names=children_names
        )

        # Save directory hierarchy
        temp_storage.save_directory_hierarchy(
            file_uri="docs",
            hierarchy=dir_hierarchy
        )

        # Verify
        loaded = temp_storage.load_directory_hierarchy(file_uri="docs")
        assert loaded is not None
        assert "3" in loaded.l0 or "文档" in loaded.l0  # Should mention count
