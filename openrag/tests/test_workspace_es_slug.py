"""Workspace slug → Elasticsearch index name helpers."""

from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    build_workspace_chunks_physical_index_name,
    build_workspace_chunks_read_alias,
    build_workspace_chunks_write_alias,
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


def test_build_v2_physical_and_alias_names():
    assert build_workspace_chunks_physical_index_name("demo", 3) == (
        "openrag_ws_demo_chunks_v2"
    )
    assert build_workspace_chunks_read_alias("demo", 3) == (
        "openrag_ws_demo_chunks_read"
    )
    assert build_workspace_chunks_write_alias("demo", 3) == (
        "openrag_ws_demo_chunks_write"
    )


def test_all_versioned_names_respect_byte_limit_and_collision_guard():
    long_slug = "a" * 300
    other_slug = "a" * 299 + "b"
    builders = [
        build_workspace_chunks_physical_index_name,
        build_workspace_chunks_read_alias,
        build_workspace_chunks_write_alias,
    ]

    for builder in builders:
        name = builder(long_slug, 1)
        assert len(name.encode("utf-8")) <= 255
        assert name != builder(other_slug, 1)
        assert name == builder(long_slug, 1)
