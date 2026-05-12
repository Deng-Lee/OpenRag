"""RAGFlow integration tests"""

import pytest
import sys
import os
from types import ModuleType


@pytest.fixture(scope="module")
def ragflow_path():
    """Setup RAGFlow path for imports with cleanup"""
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'ragflow'))

    # Add path if not already present
    path_added = False
    if path not in sys.path:
        sys.path.insert(0, path)
        path_added = True

    yield path

    # Cleanup: remove path if we added it
    if path_added and path in sys.path:
        sys.path.remove(path)


def test_import_deepdoc(ragflow_path):
    """Test importing deepdoc.parser.PdfParser from RAGFlow source"""
    try:
        from deepdoc.parser import PdfParser
    except ImportError as e:
        pytest.skip(f"RAGFlow source not available: {e}")

    assert PdfParser is not None
    assert isinstance(PdfParser, type), "PdfParser should be a class"


def test_import_rag_nlp(ragflow_path):
    """Test importing rag.nlp.rag_tokenizer from RAGFlow source"""
    try:
        from rag.nlp import rag_tokenizer
    except ImportError as e:
        pytest.skip(f"RAGFlow source not available: {e}")

    assert rag_tokenizer is not None
    assert isinstance(rag_tokenizer, ModuleType), "rag_tokenizer should be a module"


def test_pdf_parser(ragflow_path):
    """Test PDF parser functionality (skipped - requires test file)"""
    try:
        from deepdoc.parser import PdfParser
    except ImportError as e:
        pytest.skip(f"RAGFlow source not available: {e}")

    # Skip if no test PDF file available
    pytest.skip("Requires test PDF file")

    parser = PdfParser()
    assert parser is not None
    assert isinstance(parser, PdfParser), "parser should be a PdfParser instance"
