"""Full-text search helpers (Elasticsearch chunk index)."""

from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    normalize_workspace_slug_segment,
)

__all__ = [
    "build_workspace_chunks_index_name",
    "normalize_workspace_slug_segment",
]
