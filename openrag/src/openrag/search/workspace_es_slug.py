"""Normalize workspace slug for Elasticsearch index names (per design spec)."""

from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger(__name__)

# openrag_ws_{segment}_chunks — ES index name max 255 bytes
_MAX_INDEX_BYTES = 255
_PREFIX = "openrag_ws_"
_SUFFIX = "_chunks"


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
    segment = normalize_workspace_slug_segment(raw_slug, workspace_id)
    stem = f"{_PREFIX}{segment}{_SUFFIX}"
    if len(stem.encode("utf-8")) <= _MAX_INDEX_BYTES:
        return stem
    digest = hashlib.sha1(segment.encode("utf-8")).hexdigest()[:8]
    # openrag_ws_{truncated}_{digest}_chunks
    mid = f"_{digest}"
    max_seg = _MAX_INDEX_BYTES - len((_PREFIX + mid + _SUFFIX).encode("utf-8"))
    if max_seg < 1:
        return f"{_PREFIX}{digest}{_SUFFIX}"
    truncated = (
        segment.encode("utf-8")[:max_seg].decode("utf-8", errors="ignore").rstrip("-")
    )
    if not truncated:
        truncated = digest
    return f"{_PREFIX}{truncated}{mid}{_SUFFIX}"
