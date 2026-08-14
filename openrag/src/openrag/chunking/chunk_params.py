"""Resolve chunking parameters for the ingest pipeline (no file-type logic in orchestration)."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_chunk_method(file_path: str, parser_type: str) -> str:
    """Map file path + parser hint to ``chunk_method`` (forward-compatible hook).

    Values align with ``ChunkEngine`` / downstream expectations:
    ``presentation`` | ``pdf_manual`` | ``manual``.
    """
    ptype = (parser_type or "").lower()
    ext = Path(file_path).suffix.lower()
    if ptype in {"ppt", "pptx"} or (ptype == "auto" and ext in {".ppt", ".pptx"}):
        return "presentation"
    if ptype in {"pdf", "deepdoc"} or (ptype == "auto" and ext == ".pdf"):
        return "pdf_manual"
    return "manual"


def chunk_size_overlap_from_env() -> tuple[int, int]:
    """Read ``OPENRAG_CHUNK_SIZE`` / ``OPENRAG_CHUNK_OVERLAP`` with defaults."""
    size = int(os.environ.get("OPENRAG_CHUNK_SIZE", "300"))
    overlap = int(os.environ.get("OPENRAG_CHUNK_OVERLAP", "40"))
    return size, overlap


def min_chunk_tokens_from_env() -> int:
    """Read ``OPENRAG_MIN_CHUNK_TOKENS`` — minimum token count for a standalone chunk.

    Chunks below this threshold are merged into the next/previous chunk
    to avoid half-sentence fragments.  Default 32 (~64 Chinese chars).
    """
    val = os.environ.get("OPENRAG_MIN_CHUNK_TOKENS", "32")
    try:
        return max(0, int(val))
    except (ValueError, TypeError):
        return 32
