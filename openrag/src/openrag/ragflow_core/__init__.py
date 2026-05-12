"""RAGFlow-aligned parser strategy core (internal, avoids heavy parser imports)."""

from openrag.ragflow_core.compat import to_document_blocks
from openrag.ragflow_core.router import resolve_parser_strategy

__all__ = ["resolve_parser_strategy", "to_document_blocks"]
