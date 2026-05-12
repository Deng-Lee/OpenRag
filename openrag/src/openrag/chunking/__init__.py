"""Chunking module for OpenRag."""

try:
    from openrag.chunking.chunk_models import Chunk
    from openrag.chunking.chunk_engine import ChunkEngine, ChunkStrategy
except ImportError:
    from .chunk_models import Chunk
    from .chunk_engine import ChunkEngine, ChunkStrategy

__all__ = ["Chunk", "ChunkEngine", "ChunkStrategy"]
