"""Normalize workspace slug for Elasticsearch index names (per design spec)."""

from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger(__name__)

# openrag_ws_{segment}_chunks — ES index name max 255 bytes
_MAX_INDEX_BYTES = 255
_PREFIX = "openrag_ws_"
_CHUNKS_SUFFIX = "_chunks"


def normalize_workspace_slug_segment(raw_slug: str, workspace_id: int) -> str:
    """Return a lowercase [a-z0-9-] segment; empty input falls back to id{workspace_id}."""
    s = (raw_slug or "").strip().lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        logger.error(
            "Workspace slug empty after normalize; using id fallback workspace_id=%s",
            workspace_id,
        )
        return f"id{workspace_id}"
    return s


def build_workspace_chunks_index_name(raw_slug: str, workspace_id: int) -> str:
    """Full index name: openrag_ws_{segment}_chunks with byte cap and collision guard."""
    return _build_workspace_name(raw_slug, workspace_id, _CHUNKS_SUFFIX)


def build_workspace_chunks_physical_index_name(
    raw_slug: str, workspace_id: int
) -> str:
    """A02 v2 physical chunk index name."""
    return _build_workspace_name(raw_slug, workspace_id, f"{_CHUNKS_SUFFIX}_v2")


def build_workspace_chunks_read_alias(raw_slug: str, workspace_id: int) -> str:
    """A02 read alias for a workspace chunk index."""
    return _build_workspace_name(raw_slug, workspace_id, f"{_CHUNKS_SUFFIX}_read")


def build_workspace_chunks_write_alias(raw_slug: str, workspace_id: int) -> str:
    """A02 write alias for a workspace chunk index."""
    return _build_workspace_name(raw_slug, workspace_id, f"{_CHUNKS_SUFFIX}_write")


def _build_workspace_name(raw_slug: str, workspace_id: int, suffix: str) -> str:
    segment = normalize_workspace_slug_segment(raw_slug, workspace_id)
    stem = f"{_PREFIX}{segment}{suffix}"
    if len(stem.encode("utf-8")) <= _MAX_INDEX_BYTES:
        return stem
    digest = hashlib.sha1(segment.encode("utf-8")).hexdigest()[:8]
    mid = f"_{digest}"
    max_seg = _MAX_INDEX_BYTES - len((_PREFIX + mid + suffix).encode("utf-8"))
    if max_seg < 1:
        return f"{_PREFIX}{digest}{suffix}"
    truncated = (
        segment.encode("utf-8")[:max_seg].decode("utf-8", errors="ignore").rstrip("-")
    )
    if not truncated:
        truncated = digest
    return f"{_PREFIX}{truncated}{mid}{suffix}"
