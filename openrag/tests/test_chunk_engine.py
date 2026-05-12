"""Tests for chunking engine."""

import pytest
from src.openrag.parsers.base import DocumentBlock
from src.openrag.chunking.chunk_models import Chunk
from src.openrag.chunking.chunk_engine import (
    ChunkEngine,
    ChunkStrategy,
    _find_chunk_pos_robust,
)


class TestChunkModel:
    """Tests for Chunk data model."""

    def test_chunk_model_creation(self):
        """Test Chunk dataclass creation."""
        chunk = Chunk(
            text="Test chunk",
            chunk_id="test-id-123",
            page=1,
            start_offset=0,
            end_offset=10,
            bbox=(0.0, 0.0, 100.0, 50.0),
            level=0,
            block_type="text"
        )

        assert chunk.text == "Test chunk"
        assert chunk.chunk_id == "test-id-123"
        assert chunk.page == 1
        assert chunk.start_offset == 0
        assert chunk.end_offset == 10
        assert chunk.bbox == (0.0, 0.0, 100.0, 50.0)
        assert chunk.level == 0
        assert chunk.block_type == "text"
        assert chunk.parent_chunk_id is None
        assert chunk.metadata == {}

    def test_chunk_to_openviking_format(self):
        """Test conversion to OpenViking format."""
        chunk = Chunk(
            text="Test chunk",
            chunk_id="test-id-123",
            parent_chunk_id="parent-id",
            page=2,
            start_offset=100,
            end_offset=110,
            bbox=(10.0, 20.0, 110.0, 70.0),
            level=1,
            block_type="heading",
            metadata={"key": "value"}
        )

        result = chunk.to_openviking_format()

        assert result["text"] == "Test chunk"
        assert result["chunk_id"] == "test-id-123"
        assert result["parent_chunk_id"] == "parent-id"
        assert result["page"] == 2
        assert result["start_offset"] == 100
        assert result["end_offset"] == 110
        assert result["bbox"] == (10.0, 20.0, 110.0, 70.0)
        assert result["level"] == 1
        assert result["block_type"] == "heading"
        assert result["metadata"] == {"key": "value"}


class TestParagraphChunking:
    """Tests for paragraph-based chunking."""

    def test_paragraph_chunking(self):
        """Test paragraph strategy splits by double newline."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        text_blocks = [
            DocumentBlock(
                text="First paragraph.\n\nSecond paragraph.\n\nThird paragraph.",
                page=1,
                offset=0,
                bbox=(0.0, 0.0, 100.0, 100.0),
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks)

        assert len(chunks) == 3
        assert chunks[0].text == "First paragraph."
        assert chunks[1].text == "Second paragraph."
        assert chunks[2].text == "Third paragraph."

    def test_paragraph_chunking_preserves_position(self):
        """Verify position info preserved in paragraph chunking."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        text_blocks = [
            DocumentBlock(
                text="Para one.\n\nPara two.",
                page=2,
                offset=50,
                bbox=(10.0, 20.0, 110.0, 70.0),
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks)

        assert len(chunks) == 2

        # First chunk
        assert chunks[0].page == 2
        assert chunks[0].start_offset == 50
        assert chunks[0].end_offset == 59  # 50 + len("Para one.")
        assert chunks[0].bbox == (10.0, 20.0, 110.0, 70.0)
        assert chunks[0].level == 0
        assert chunks[0].block_type == "text"

        # Second chunk
        assert chunks[1].page == 2
        assert chunks[1].start_offset == 61  # 50 + len("Para one.") + 2 for \n\n
        assert chunks[1].bbox == (10.0, 20.0, 110.0, 70.0)


class TestFixedSizeChunking:
    """Tests for fixed-size chunking."""

    def test_fixed_size_chunking(self):
        """Test fixed size strategy."""
        engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)

        text_blocks = [
            DocumentBlock(
                text="A" * 100,
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks, chunk_size=30, chunk_overlap=5)

        assert len(chunks) > 1
        assert all(len(chunk.text) <= 30 for chunk in chunks)

    def test_fixed_size_chunking_with_overlap(self):
        """Test overlap works in fixed size chunking."""
        engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)

        text_blocks = [
            DocumentBlock(
                text="ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks, chunk_size=10, chunk_overlap=3)

        assert len(chunks) >= 2
        # Verify chunks have expected sizes
        for chunk in chunks[:-1]:  # All but last
            assert len(chunk.text) <= 10

    def test_fixed_size_preserves_position(self):
        """Verify position info preserved in fixed size chunking."""
        engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)

        text_blocks = [
            DocumentBlock(
                text="Hello world test",
                page=3,
                offset=100,
                bbox=(5.0, 5.0, 50.0, 50.0),
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks, chunk_size=50, chunk_overlap=5)

        assert len(chunks) >= 1
        assert chunks[0].page == 3
        assert chunks[0].start_offset == 100
        assert chunks[0].bbox == (5.0, 5.0, 50.0, 50.0)
        assert chunks[0].block_type == "text"
        assert chunks[0].level == 0


class TestSemanticChunking:
    """Tests for semantic chunking."""

    def test_semantic_chunking_fallback(self):
        """Test semantic falls back to fixed size."""
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)

        text_blocks = [
            DocumentBlock(
                text="A" * 100,
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks, chunk_size=30, chunk_overlap=5)

        # Should behave like fixed size chunking
        assert len(chunks) > 1
        assert all(len(chunk.text) <= 30 for chunk in chunks)

    def test_semantic_children_delimiter_split(self, monkeypatch):
        """Semantic chunking supports children delimiter second split."""
        monkeypatch.setenv("OPENRAG_CHILDREN_DELIMITER", "`##`")
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        text_blocks = [
            DocumentBlock(
                text="part1##part2##part3",
                page=1,
                offset=0,
                block_type="text",
                level=0,
            )
        ]

        chunks = engine.chunk(text_blocks, chunk_size=512, chunk_overlap=0)
        assert len(chunks) == 3
        assert chunks[0].text == "part1##"
        assert chunks[1].text == "part2##"
        assert chunks[2].text == "part3"

    def test_semantic_docx_like_mixed_block_types(self):
        """Semantic chunking keeps table/image chunks separated."""
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        text_blocks = [
            DocumentBlock(
                text="normal text",
                page=1,
                offset=0,
                block_type="text",
                level=0,
            ),
            DocumentBlock(
                text="table row a | table row b",
                page=1,
                offset=20,
                block_type="table",
                level=0,
            ),
            DocumentBlock(
                text="figure caption",
                page=1,
                offset=40,
                block_type="image",
                level=0,
                image=b"mock",
            ),
        ]

        chunks = engine.chunk(text_blocks, chunk_size=512, chunk_overlap=0)
        block_types = [c.block_type for c in chunks]
        assert "table" in block_types
        assert "image" in block_types
        assert any(c.metadata.get("doc_type_kwd") == "table" for c in chunks)
        assert any(c.metadata.get("doc_type_kwd") == "image" for c in chunks)

    def test_semantic_adds_ragflow_token_fields(self):
        """Semantic chunk metadata includes RagFlow tokenized fields."""
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        text_blocks = [
            DocumentBlock(
                text="hello <table><tr><td>world</td></tr></table>",
                page=1,
                offset=0,
                block_type="text",
                level=0,
            )
        ]
        chunks = engine.chunk(text_blocks, chunk_size=512, chunk_overlap=0)
        assert len(chunks) >= 1
        md = chunks[0].metadata
        assert "content_with_weight" in md
        assert "content_ltks" in md
        assert "content_sm_ltks" in md
        assert md["content_with_weight"].startswith("hello")

    def test_semantic_adds_ragflow_position_fields(self):
        """Semantic chunk metadata includes RagFlow add_positions fields."""
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        text_blocks = [
            DocumentBlock(
                text="position test chunk",
                page=3,
                offset=0,
                bbox=(10.0, 20.0, 30.0, 40.0),
                block_type="text",
                level=0,
            )
        ]
        chunks = engine.chunk(text_blocks, chunk_size=512, chunk_overlap=0)
        assert len(chunks) >= 1
        md = chunks[0].metadata
        assert md["page_num_int"] == [3]
        assert md["top_int"] == [20]
        assert md["position_int"][0] == (3, 10, 30, 20, 40)

    def test_semantic_children_split_sets_mom_with_weight(self, monkeypatch):
        """Children split chunk should carry mom_with_weight like RagFlow."""
        monkeypatch.setenv("OPENRAG_CHILDREN_DELIMITER", "`##`")
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        text_blocks = [
            DocumentBlock(
                text="alpha##beta",
                page=1,
                offset=0,
                block_type="text",
                level=0,
            )
        ]
        chunks = engine.chunk(text_blocks, chunk_size=512, chunk_overlap=0)
        assert len(chunks) == 2
        assert chunks[0].metadata.get("mom_with_weight") == "alpha##beta"
        assert chunks[1].metadata.get("mom_with_weight") == "alpha##beta"

    def test_semantic_owner_bbox_follows_source_block(self, monkeypatch):
        """Different semantic chunks should map to their own source bbox."""
        monkeypatch.setenv("OPENRAG_CHUNK_DELIMITER", "`@@@`")
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        text_blocks = [
            DocumentBlock(
                text="block-one",
                page=1,
                offset=0,
                bbox=(10.0, 10.0, 20.0, 20.0),
                block_type="text",
                level=0,
            ),
            DocumentBlock(
                text="block-two",
                page=1,
                offset=100,
                bbox=(30.0, 30.0, 40.0, 40.0),
                block_type="text",
                level=0,
            ),
        ]
        chunks = engine.chunk(text_blocks, chunk_size=512, chunk_overlap=0)
        assert len(chunks) >= 2
        assert chunks[0].bbox == (10.0, 10.0, 20.0, 20.0)
        assert chunks[1].bbox == (30.0, 30.0, 40.0, 40.0)

    def test_semantic_bbox_union_for_merged_blocks(self):
        """Merged semantic chunk uses bbox union on same page."""
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        text_blocks = [
            DocumentBlock(
                text="aaa",
                page=1,
                offset=0,
                bbox=(10.0, 10.0, 20.0, 20.0),
                block_type="text",
                level=0,
            ),
            DocumentBlock(
                text="bbb",
                page=1,
                offset=10,
                bbox=(30.0, 30.0, 40.0, 40.0),
                block_type="text",
                level=0,
            ),
        ]
        chunks = engine.chunk(text_blocks, chunk_size=512, chunk_overlap=0)
        assert len(chunks) >= 1
        # Usually one merged chunk: "aaa\nbbb"; bbox should cover both.
        assert chunks[0].bbox == (10.0, 10.0, 40.0, 40.0)


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_text_blocks(self):
        """Handle empty input."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        chunks = engine.chunk([])

        assert chunks == []

    def test_single_text_block(self):
        """Handle single block."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        text_blocks = [
            DocumentBlock(
                text="Single block",
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks)

        assert len(chunks) == 1
        assert chunks[0].text == "Single block"

    def test_multiple_pages(self):
        """Handle multi-page documents."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        text_blocks = [
            DocumentBlock(
                text="Page one content",
                page=1,
                offset=0,
                block_type="text",
                level=0
            ),
            DocumentBlock(
                text="Page two content",
                page=2,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks)

        assert len(chunks) == 2
        assert chunks[0].page == 1
        assert chunks[1].page == 2

    def test_chunk_id_uniqueness(self):
        """Verify unique IDs generated."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        text_blocks = [
            DocumentBlock(
                text="First.\n\nSecond.\n\nThird.",
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks)

        chunk_ids = [chunk.chunk_id for chunk in chunks]
        assert len(chunk_ids) == len(set(chunk_ids))  # All unique


class TestInputValidation:
    """Tests for input validation."""

    def test_chunk_size_zero(self):
        """Should raise ValueError for chunk_size=0."""
        engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)

        text_blocks = [
            DocumentBlock(
                text="Test text",
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        with pytest.raises(ValueError, match="chunk_size must be greater than 0"):
            engine.chunk(text_blocks, chunk_size=0, chunk_overlap=0)

    def test_chunk_size_negative(self):
        """Should raise ValueError for negative chunk_size."""
        engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)

        text_blocks = [
            DocumentBlock(
                text="Test text",
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        with pytest.raises(ValueError, match="chunk_size must be greater than 0"):
            engine.chunk(text_blocks, chunk_size=-10, chunk_overlap=0)

    def test_chunk_overlap_negative(self):
        """Should raise ValueError for negative chunk_overlap."""
        engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)

        text_blocks = [
            DocumentBlock(
                text="Test text",
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        with pytest.raises(ValueError, match="chunk_overlap must be non-negative"):
            engine.chunk(text_blocks, chunk_size=100, chunk_overlap=-5)

    def test_chunk_overlap_exceeds_size(self):
        """Should raise ValueError when chunk_overlap >= chunk_size."""
        engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)

        text_blocks = [
            DocumentBlock(
                text="Test text",
                page=1,
                offset=0,
                block_type="text",
                level=0
            )
        ]

        with pytest.raises(ValueError, match="chunk_overlap.*must be less than chunk_size"):
            engine.chunk(text_blocks, chunk_size=50, chunk_overlap=50)

        with pytest.raises(ValueError, match="chunk_overlap.*must be less than chunk_size"):
            engine.chunk(text_blocks, chunk_size=50, chunk_overlap=60)

    def test_none_text_blocks(self):
        """Should raise ValueError for None text_blocks."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        with pytest.raises(ValueError, match="text_blocks cannot be None"):
            engine.chunk(None)


class TestChunkSearchRobustness:
    """Tests for robust chunk text location."""

    def test_find_chunk_pos_robust_with_whitespace_variation(self):
        full = "alpha  beta\ngamma"
        chunk = "alpha beta gamma"
        pos = _find_chunk_pos_robust(full, chunk, 0)
        assert pos == 0


class TestChunkMethodSplit:
    """Environment-controlled split behavior."""

    def test_children_delimiter_enables_split(self, monkeypatch):
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        monkeypatch.setenv("OPENRAG_CHILDREN_DELIMITER", r"\n#{1,6}\s|\n[-*+]\s|\n\d+\.\s|\n\n")
        text_blocks = [
            DocumentBlock(
                text="# title\nline a\n\n- bullet b\nline c",
                page=1,
                offset=0,
                block_type="text",
                level=0,
            )
        ]
        chunks = engine.chunk(text_blocks, chunk_size=256, chunk_overlap=0)
        assert len(chunks) >= 2

    def test_pdf_like_pattern_splits_content(self, monkeypatch):
        engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
        monkeypatch.setenv(
            "OPENRAG_CHILDREN_DELIMITER",
            r"\n•\s|\n[-*+]\s|\n\d+\.\s|\n(?=[^\n]{1,40}：)|\n+",
        )
        text_blocks = [
            DocumentBlock(
                text="前置条件：A\n• Docker Desktop\n资源需求：2C4G",
                page=1,
                offset=0,
                block_type="text",
                level=0,
            )
        ]
        chunks = engine.chunk(text_blocks, chunk_size=256, chunk_overlap=0)
        assert len(chunks) >= 3

    def test_paragraph_offset_accuracy(self):
        """Verify offsets match stripped text in paragraph chunking."""
        engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)

        text_blocks = [
            DocumentBlock(
                text="  First para  \n\n  Second para  ",
                page=1,
                offset=100,
                block_type="text",
                level=0
            )
        ]

        chunks = engine.chunk(text_blocks)

        assert len(chunks) == 2
        # First chunk: stripped text is "First para"
        assert chunks[0].text == "First para"
        assert chunks[0].start_offset == 100
        assert chunks[0].end_offset == 100 + len("First para")

        # Second chunk: stripped text is "Second para"
        assert chunks[1].text == "Second para"
        # Offset should account for "  First para  " (14 chars) + "\n\n" (2 chars) = 116
        assert chunks[1].start_offset == 116
        assert chunks[1].end_offset == 116 + len("Second para")

