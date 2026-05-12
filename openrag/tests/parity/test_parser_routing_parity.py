"""Parity tests: parser routing matches RAGFlow logical strategies."""

import pytest

from openrag.parsers.adapters.json_adapter import JsonParserAdapter
from openrag.parsers.adapters.markdown_adapter import MarkdownParserAdapter
from openrag.parsers.adapters.txt_adapter import TxtParserAdapter
from openrag.parsers.factory import ParserFactory
from openrag.ragflow_core.router import resolve_parser_strategy


def test_resolve_parser_strategy_supports_ragflow_matrix():
    assert resolve_parser_strategy("a.pdf") == "pdf"
    assert resolve_parser_strategy("a.doc") == "doc"
    assert resolve_parser_strategy("a.docx") == "docx"
    assert resolve_parser_strategy("a.mdx") == "markdown"
    assert resolve_parser_strategy("a.jsonl") == "json"
    assert resolve_parser_strategy("a.ldjson") == "json"
    assert resolve_parser_strategy("a.py") == "text"


def test_jsonl_and_ldjson_map_to_json_strategy():
    """Logical strategy key matches factory JSON adapter (json / jsonl / ldjson)."""
    assert resolve_parser_strategy("dir/data.jsonl") == "json"
    assert resolve_parser_strategy("dir/data.ldjson") == "json"


def test_resolve_parser_strategy_unknown_extension_raises():
    with pytest.raises(ValueError, match="Unsupported extension"):
        resolve_parser_strategy("a.unknown")


def test_factory_auto_accepts_mdx_jsonl_py_js():
    factory = ParserFactory()
    assert isinstance(factory.get_parser("a.mdx"), MarkdownParserAdapter)
    assert isinstance(factory.get_parser("a.jsonl"), JsonParserAdapter)
    assert isinstance(factory.get_parser("a.ldjson"), JsonParserAdapter)
    assert isinstance(factory.get_parser("a.py"), TxtParserAdapter)
    assert isinstance(factory.get_parser("a.js"), TxtParserAdapter)


def test_get_parser_by_type_extended_keys():
    factory = ParserFactory()
    assert isinstance(factory.get_parser_by_type("mdx"), MarkdownParserAdapter)
    assert isinstance(factory.get_parser_by_type("jsonl"), JsonParserAdapter)
    assert isinstance(factory.get_parser_by_type("ldjson"), JsonParserAdapter)
    assert isinstance(factory.get_parser_by_type("py"), TxtParserAdapter)
