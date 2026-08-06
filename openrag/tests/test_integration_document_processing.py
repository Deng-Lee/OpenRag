"""Integration tests for complete document processing pipeline.

Tests the end-to-end flow: parse → chunk → embed → store
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch

from src.openrag.parsers.base import DocumentParser, DocumentBlock
from src.openrag.parsers.parser_registry import ParserRegistry
from src.openrag.chunking.chunk_engine import ChunkEngine, ChunkStrategy
from src.openrag.chunking.chunk_models import Chunk
from src.openrag.embedding.embedding_engine import EmbeddingEngine
from src.openrag.processors.document_processor import DocumentProcessor


class MockPDFParser(DocumentParser):
    """Mock PDF parser for testing."""

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """Parse mock PDF file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        # Simulate PDF with multiple pages and blocks
        return [
            DocumentBlock(
                text="Introduction\n\nThis is the introduction paragraph.",
                page=1,
                offset=0,
                bbox=(10.0, 10.0, 200.0, 50.0),
                block_type="text",
                level=1
            ),
            DocumentBlock(
                text="The main content starts here. This is a longer paragraph with more details.",
                page=1,
                offset=100,
                bbox=(10.0, 60.0, 200.0, 100.0),
                block_type="text",
                level=0
            ),
            DocumentBlock(
                text="Section 2\n\nAnother section with different content.",
                page=2,
                offset=0,
                bbox=(10.0, 10.0, 200.0, 50.0),
                block_type="text",
                level=1
            )
        ]

    def supports(self, file_path: str) -> bool:
        """Check if file is PDF."""
        return file_path.lower().endswith('.pdf')


class MockMarkdownParser(DocumentParser):
    """Mock Markdown parser for testing."""

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """Parse mock Markdown file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Simple markdown parsing - split by headers
        blocks = []
        lines = content.split('\n')
        current_text = []
        current_level = 0
        offset = 0

        for line in lines:
            if line.startswith('#'):
                # Save previous block
                if current_text:
                    text = '\n'.join(current_text)
                    blocks.append(DocumentBlock(
                        text=text,
                        page=1,
                        offset=offset - len(text),
                        block_type="text",
                        level=current_level
                    ))
                    current_text = []

                # Parse header
                level = len(line) - len(line.lstrip('#'))
                current_level = level
                current_text.append(line)
            else:
                current_text.append(line)

            offset += len(line) + 1

        # Add final block
        if current_text:
            text = '\n'.join(current_text)
            blocks.append(DocumentBlock(
                text=text,
                page=1,
                offset=offset - len(text),
                block_type="text",
                level=current_level
            ))

        return blocks

    def supports(self, file_path: str) -> bool:
        """Check if file is Markdown."""
        return file_path.lower().endswith(('.md', '.markdown'))


@pytest.fixture
def temp_dir():
    """Create temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def mock_db():
    """Mock database session."""
    return Mock()


@pytest.fixture
def parser_registry():
    """Create parser registry with mock parsers."""
    registry = ParserRegistry()
    registry.register(MockPDFParser())
    registry.register(MockMarkdownParser())
    return registry


@pytest.fixture
def document_processor_paragraph(mock_db, parser_registry):
    """Create document processor with paragraph chunking."""
    chunk_engine = ChunkEngine(strategy=ChunkStrategy.PARAGRAPH)
    embedding_engine = EmbeddingEngine(cache_enabled=True)
    return DocumentProcessor(
        db=mock_db,
        parser_registry=parser_registry,
        chunk_engine=chunk_engine,
        embedding_engine=embedding_engine,
        fulltext_required=False,
    )


@pytest.fixture
def document_processor_semantic(mock_db, parser_registry):
    """Create document processor with semantic chunking."""
    chunk_engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
    embedding_engine = EmbeddingEngine(cache_enabled=True)
    return DocumentProcessor(
        db=mock_db,
        parser_registry=parser_registry,
        chunk_engine=chunk_engine,
        embedding_engine=embedding_engine,
        fulltext_required=False,
    )


@pytest.fixture
def document_processor_fixed(mock_db, parser_registry):
    """Create document processor with fixed-size chunking."""
    chunk_engine = ChunkEngine(strategy=ChunkStrategy.FIXED_SIZE)
    embedding_engine = EmbeddingEngine(cache_enabled=True)
    return DocumentProcessor(
        db=mock_db,
        parser_registry=parser_registry,
        chunk_engine=chunk_engine,
        embedding_engine=embedding_engine,
        fulltext_required=False,
    )


def test_complete_pipeline_pdf(document_processor_paragraph, temp_dir):
    """Test complete PDF processing pipeline end-to-end."""
    # Create mock PDF file
    pdf_path = os.path.join(temp_dir, "test.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf content")

    # Process document
    result = document_processor_paragraph.process_document(
        file_path=pdf_path,
        file_id=1,
        user_id=100
    )

    # Verify result structure
    assert result["file_id"] == 1
    assert result["file_path"] == pdf_path
    assert result["status"] == "processed"

    # Verify pipeline stages
    assert result["text_blocks"] == 3  # MockPDFParser returns 3 blocks
    assert result["chunks"] > 0  # Should have chunks
    assert result["embeddings"] > 0  # Should have embeddings
    assert result["embeddings"] == result["chunks"]  # One embedding per chunk


def test_complete_pipeline_markdown(document_processor_paragraph, temp_dir):
    """Test complete Markdown processing pipeline end-to-end."""
    # Create mock Markdown file
    md_path = os.path.join(temp_dir, "test.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write("""# Introduction

This is the introduction paragraph.

## Section 1

Content for section 1.

## Section 2

Content for section 2 with more details.
""")

    # Process document
    result = document_processor_paragraph.process_document(
        file_path=md_path,
        file_id=2,
        user_id=200
    )

    # Verify result
    assert result["file_id"] == 2
    assert result["file_path"] == md_path
    assert result["status"] == "processed"
    assert result["text_blocks"] > 0
    assert result["chunks"] > 0
    assert result["embeddings"] > 0


def test_paragraph_chunking_strategy(document_processor_paragraph, temp_dir):
    """Test pipeline with paragraph chunking strategy."""
    pdf_path = os.path.join(temp_dir, "test_para.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    result = document_processor_paragraph.process_document(
        file_path=pdf_path,
        file_id=3,
        user_id=300
    )

    # Paragraph strategy should create chunks based on double newlines
    assert result["chunks"] > 0
    assert result["status"] == "processed"


def test_semantic_chunking_strategy(document_processor_semantic, temp_dir):
    """Test pipeline with semantic chunking strategy."""
    pdf_path = os.path.join(temp_dir, "test_semantic.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    result = document_processor_semantic.process_document(
        file_path=pdf_path,
        file_id=4,
        user_id=400
    )

    # Semantic strategy (currently falls back to fixed size)
    assert result["chunks"] > 0
    assert result["status"] == "processed"


def test_fixed_size_chunking_strategy(document_processor_fixed, temp_dir):
    """Test pipeline with fixed-size chunking strategy."""
    pdf_path = os.path.join(temp_dir, "test_fixed.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    result = document_processor_fixed.process_document(
        file_path=pdf_path,
        file_id=5,
        user_id=500
    )

    # Fixed size strategy should create chunks of consistent size
    assert result["chunks"] > 0
    assert result["status"] == "processed"


def test_position_preservation_through_pipeline(document_processor_paragraph, temp_dir):
    """Verify position information is preserved through the pipeline."""
    pdf_path = os.path.join(temp_dir, "test_position.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    # Get the components to inspect intermediate results
    processor = document_processor_paragraph

    # Step 1: Parse
    text_blocks = processor.parser_registry.parse(pdf_path)
    assert len(text_blocks) == 3

    # Verify text blocks have position info
    for block in text_blocks:
        assert block.page > 0
        assert block.offset >= 0
        assert hasattr(block, 'bbox')
        assert hasattr(block, 'level')
        assert hasattr(block, 'block_type')

    # Step 2: Chunk
    chunks = processor.chunk_engine.chunk(text_blocks)
    assert len(chunks) > 0

    # Verify chunks preserve position info
    for chunk in chunks:
        assert chunk.page > 0
        assert chunk.start_offset >= 0
        assert chunk.end_offset >= chunk.start_offset
        assert hasattr(chunk, 'bbox')
        assert hasattr(chunk, 'level')
        assert hasattr(chunk, 'block_type')

    # Step 3: Embed
    chunk_embeddings = processor.embedding_engine.embed_chunks(chunks)
    assert len(chunk_embeddings) == len(chunks)

    # Verify embeddings maintain chunk reference
    for chunk, embedding in chunk_embeddings:
        assert isinstance(chunk, Chunk)
        assert isinstance(embedding, list)
        assert len(embedding) == 1536  # OpenAI ada-002 dimension
        # Position info still accessible through chunk
        assert chunk.page > 0
        assert chunk.start_offset >= 0


def test_error_handling_missing_file(document_processor_paragraph, temp_dir):
    """Test error handling for missing files."""
    missing_path = os.path.join(temp_dir, "nonexistent.pdf")

    with pytest.raises(FileNotFoundError):
        document_processor_paragraph.process_document(
            file_path=missing_path,
            file_id=6,
            user_id=600
        )


def test_error_handling_unsupported_format(document_processor_paragraph, temp_dir):
    """Test error handling for unsupported file types."""
    unsupported_path = os.path.join(temp_dir, "test.xyz")
    with open(unsupported_path, 'w') as f:
        f.write("unsupported content")

    with pytest.raises(ValueError, match="No parser found"):
        document_processor_paragraph.process_document(
            file_path=unsupported_path,
            file_id=7,
            user_id=700
        )


def test_error_handling_empty_file(document_processor_paragraph, temp_dir):
    """Test handling of empty files."""
    # Create empty markdown file
    empty_path = os.path.join(temp_dir, "empty.md")
    with open(empty_path, 'w') as f:
        f.write("")

    result = document_processor_paragraph.process_document(
        file_path=empty_path,
        file_id=8,
        user_id=800
    )

    # Should handle gracefully
    assert result["status"] == "processed"
    assert result["text_blocks"] >= 0
    assert result["chunks"] >= 0


def test_data_flow_between_components(document_processor_paragraph, temp_dir):
    """Verify correct data flow between all components."""
    pdf_path = os.path.join(temp_dir, "test_flow.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    processor = document_processor_paragraph

    # Step 1: Parse - should return DocumentBlock objects
    text_blocks = processor.parser_registry.parse(pdf_path)
    assert all(isinstance(block, DocumentBlock) for block in text_blocks)

    # Step 2: Chunk - should accept DocumentBlocks and return Chunks
    chunks = processor.chunk_engine.chunk(text_blocks)
    assert all(isinstance(chunk, Chunk) for chunk in chunks)

    # Step 3: Embed - should accept Chunks and return (Chunk, embedding) tuples
    chunk_embeddings = processor.embedding_engine.embed_chunks(chunks)
    assert all(isinstance(item, tuple) for item in chunk_embeddings)
    assert all(isinstance(item[0], Chunk) for item in chunk_embeddings)
    assert all(isinstance(item[1], list) for item in chunk_embeddings)

    # Verify data integrity through pipeline
    assert len(chunks) == len(chunk_embeddings)


def test_embedding_caching(document_processor_paragraph, temp_dir):
    """Test that embedding engine caches results correctly."""
    pdf_path = os.path.join(temp_dir, "test_cache.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    processor = document_processor_paragraph
    embedding_engine = processor.embedding_engine

    # Clear cache
    embedding_engine.clear_cache()
    assert embedding_engine.get_cache_size() == 0

    # Process document
    result = processor.process_document(
        file_path=pdf_path,
        file_id=9,
        user_id=900
    )

    # Cache should have entries
    cache_size = embedding_engine.get_cache_size()
    assert cache_size > 0
    assert cache_size == result["embeddings"]


def test_multiple_documents_processing(document_processor_paragraph, temp_dir):
    """Test processing multiple documents in sequence."""
    results = []

    for i in range(3):
        file_path = os.path.join(temp_dir, f"test_{i}.pdf")
        with open(file_path, 'w') as f:
            f.write(f"mock pdf {i}")

        result = document_processor_paragraph.process_document(
            file_path=file_path,
            file_id=10 + i,
            user_id=1000 + i
        )
        results.append(result)

    # Verify all processed successfully
    assert len(results) == 3
    assert all(r["status"] == "processed" for r in results)
    assert all(r["chunks"] > 0 for r in results)


def test_large_document_processing(document_processor_fixed, temp_dir):
    """Test processing a larger document with many blocks."""
    md_path = os.path.join(temp_dir, "large.md")

    # Create a larger markdown file
    content = []
    for i in range(20):
        content.append(f"## Section {i}\n")
        content.append(f"This is content for section {i}. " * 10)
        content.append("\n\n")

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(''.join(content))

    result = document_processor_fixed.process_document(
        file_path=md_path,
        file_id=13,
        user_id=1300
    )

    # Should handle large documents
    assert result["status"] == "processed"
    assert result["chunks"] > 10  # Should have many chunks


def test_bbox_preservation(document_processor_paragraph, temp_dir):
    """Test that bounding box information is preserved."""
    pdf_path = os.path.join(temp_dir, "test_bbox.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    processor = document_processor_paragraph

    # Parse and get blocks with bbox
    text_blocks = processor.parser_registry.parse(pdf_path)
    blocks_with_bbox = [b for b in text_blocks if b.bbox is not None]

    if blocks_with_bbox:
        # Chunk and verify bbox preserved
        chunks = processor.chunk_engine.chunk(text_blocks)
        chunks_with_bbox = [c for c in chunks if c.bbox is not None]

        # At least some chunks should have bbox
        assert len(chunks_with_bbox) > 0

        # Verify bbox format
        for chunk in chunks_with_bbox:
            assert isinstance(chunk.bbox, tuple)
            assert len(chunk.bbox) == 4
            assert all(isinstance(x, float) for x in chunk.bbox)


def test_level_preservation(document_processor_paragraph, temp_dir):
    """Test that heading level information is preserved."""
    pdf_path = os.path.join(temp_dir, "test_level.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    processor = document_processor_paragraph

    # Parse and get blocks with levels
    text_blocks = processor.parser_registry.parse(pdf_path)

    # Verify blocks have level info
    assert any(b.level > 0 for b in text_blocks)

    # Chunk and verify level preserved
    chunks = processor.chunk_engine.chunk(text_blocks)

    # Verify chunks preserve level
    assert all(hasattr(c, 'level') for c in chunks)
    assert any(c.level > 0 for c in chunks)


def test_block_type_preservation(document_processor_paragraph, temp_dir):
    """Test that block type information is preserved."""
    pdf_path = os.path.join(temp_dir, "test_type.pdf")
    with open(pdf_path, 'w') as f:
        f.write("mock pdf")

    processor = document_processor_paragraph

    # Parse and get blocks
    text_blocks = processor.parser_registry.parse(pdf_path)

    # Verify blocks have type info
    assert all(hasattr(b, 'block_type') for b in text_blocks)
    assert all(b.block_type == "text" for b in text_blocks)

    # Chunk and verify type preserved
    chunks = processor.chunk_engine.chunk(text_blocks)

    # Verify chunks preserve type
    assert all(hasattr(c, 'block_type') for c in chunks)
    assert all(c.block_type == "text" for c in chunks)


@pytest.mark.skip(reason="OpenViking integration not yet implemented - pending Plan 3 completion")
def test_openviking_integration(document_processor_paragraph, temp_dir):
    """
    Test OpenViking integration (TreeBuilder and VikingFS).

    This test is currently skipped because OpenViking integration
    is marked as TODO in document_processor.py. Once OpenViking
    TreeBuilder and VikingFS are integrated, this test should:
    - Verify chunks are stored in OpenViking hierarchical structure
    - Verify VikingFS URI generation
    - Verify TreeBuilder creates proper hierarchy
    """
    # TODO: Implement when OpenViking integration is complete
    pass
