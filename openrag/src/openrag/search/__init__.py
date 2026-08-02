"""Full-text search helpers (Elasticsearch chunk index)."""

from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    build_workspace_chunks_physical_index_name,
    build_workspace_chunks_read_alias,
    build_workspace_chunks_write_alias,
    normalize_workspace_slug_segment,
)

__all__ = [
    "build_workspace_chunks_index_name",
    "build_workspace_chunks_physical_index_name",
    "build_workspace_chunks_read_alias",
    "build_workspace_chunks_write_alias",
    "normalize_workspace_slug_segment",
]
