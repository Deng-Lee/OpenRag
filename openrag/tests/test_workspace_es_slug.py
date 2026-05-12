"""Workspace slug → Elasticsearch index name helpers."""

from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    normalize_workspace_slug_segment,
)


def test_normalize_slug_basic():
    assert normalize_workspace_slug_segment("Acme-Corp", 1) == "acme-corp"


def test_normalize_slug_fallback_id():
    assert normalize_workspace_slug_segment("", 42) == "id42"


def test_build_index_name_roundtrip():
    name = build_workspace_chunks_index_name("demo", 3)
    assert name == "openrag_ws_demo_chunks"


def test_build_index_name_long_slug_truncates_with_hash():
    long_slug = "a" * 300
    name = build_workspace_chunks_index_name(long_slug, 1)
    assert name.startswith("openrag_ws_")
    assert name.endswith("_chunks")
    assert len(name.encode("utf-8")) <= 255
