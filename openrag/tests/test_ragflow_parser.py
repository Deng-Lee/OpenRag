"""Tests for RAGFlow parser wrappers."""

import pytest
from openrag.parsers import (
    DocumentBlock,
    RAGFlowPDFParser,
    RAGFlowMarkdownParser,
    ParserRegistry,
)


def test_text_block_creation():
    """Test DocumentBlock dataclass."""
    block = DocumentBlock(
        text="Hello world",
        page=1,
        offset=0,
        bbox=(10.0, 20.0, 100.0, 30.0),
        block_type="text",
        level=0
    )
    assert block.text == "Hello world"
    assert block.page == 1
    assert block.offset == 0
    assert block.bbox == (10.0, 20.0, 100.0, 30.0)
    assert block.block_type == "text"
    assert block.level == 0


def test_text_block_defaults():
    """Test DocumentBlock default values."""
    block = DocumentBlock(text="Test", page=1, offset=0)
    assert block.bbox is None
    assert block.block_type == "text"
    assert block.level == 0


def test_pdf_parser_supports():
    """Test PDF parser file type detection."""
    parser = RAGFlowPDFParser()
    assert parser.supports("document.pdf") == True
    assert parser.supports("document.PDF") == True
    assert parser.supports("DOCUMENT.Pdf") == True
    assert parser.supports("document.txt") == False
    assert parser.supports("document.md") == False


def test_markdown_parser_supports():
    """Test Markdown parser file type detection."""
    parser = RAGFlowMarkdownParser()
    assert parser.supports("readme.md") == True
    assert parser.supports("README.MD") == True
    assert parser.supports("readme.markdown") == True
    assert parser.supports("README.MARKDOWN") == True
    assert parser.supports("readme.txt") == False
    assert parser.supports("readme.pdf") == False


def test_pdf_parser_not_implemented():
    """Test PDF parser raises NotImplementedError (placeholder)."""
    parser = RAGFlowPDFParser()
    with pytest.raises(NotImplementedError, match="RAGFlow source installation"):
        parser.parse("test.pdf")


def test_markdown_parser_not_implemented():
    """Test Markdown parser raises NotImplementedError (placeholder)."""
    parser = RAGFlowMarkdownParser()
    with pytest.raises(NotImplementedError):
        parser.parse("test.md")


def test_parser_registry_register():
    """Test parser registration."""
    registry = ParserRegistry()
    parser = RAGFlowPDFParser()
    registry.register(parser)
    assert len(registry.parsers) == 1
    assert registry.parsers[0] is parser


def test_parser_registry_register_multiple():
    """Test registering multiple parsers."""
    registry = ParserRegistry()
    pdf_parser = RAGFlowPDFParser()
    md_parser = RAGFlowMarkdownParser()

    registry.register(pdf_parser)
    registry.register(md_parser)

    assert len(registry.parsers) == 2
    assert pdf_parser in registry.parsers
    assert md_parser in registry.parsers


def test_parser_registry_get_parser():
    """Test getting parser by file type."""
    registry = ParserRegistry()
    registry.register(RAGFlowPDFParser())
    registry.register(RAGFlowMarkdownParser())

    pdf_parser = registry.get_parser("doc.pdf")
    assert isinstance(pdf_parser, RAGFlowPDFParser)

    md_parser = registry.get_parser("readme.md")
    assert isinstance(md_parser, RAGFlowMarkdownParser)

    no_parser = registry.get_parser("file.txt")
    assert no_parser is None


def test_parser_registry_get_parser_case_insensitive():
    """Test parser selection is case insensitive."""
    registry = ParserRegistry()
    registry.register(RAGFlowPDFParser())

    parser = registry.get_parser("DOC.PDF")
    assert isinstance(parser, RAGFlowPDFParser)


def test_parser_registry_parse_no_parser():
    """Test parsing with no suitable parser raises error."""
    registry = ParserRegistry()
    with pytest.raises(ValueError, match="No parser found"):
        registry.parse("unknown.xyz")


def test_parser_registry_parse_with_parser():
    """Test parsing delegates to appropriate parser."""
    registry = ParserRegistry()
    registry.register(RAGFlowPDFParser())

    # Should raise NotImplementedError from the parser
    with pytest.raises(NotImplementedError):
        registry.parse("test.pdf")


def test_parser_registry_first_match_wins():
    """Test that first matching parser is returned."""
    registry = ParserRegistry()
    parser1 = RAGFlowPDFParser()
    parser2 = RAGFlowPDFParser()

    registry.register(parser1)
    registry.register(parser2)

    result = registry.get_parser("test.pdf")
    assert result is parser1
