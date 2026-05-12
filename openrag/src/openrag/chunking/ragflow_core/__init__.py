"""RAGFlow-aligned chunking core (semantic merge, docx-like, children split)."""

from openrag.chunking.ragflow_core.metadata import (
    build_chunk_metadata,
    ragflow_add_positions_fields,
    ragflow_tokenize_fields,
)
from openrag.chunking.ragflow_core.semantic import (
    chunk_semantic_ragflow,
    find_chunk_pos_robust,
)

__all__ = [
    "build_chunk_metadata",
    "chunk_semantic_ragflow",
    "find_chunk_pos_robust",
    "ragflow_add_positions_fields",
    "ragflow_tokenize_fields",
]
