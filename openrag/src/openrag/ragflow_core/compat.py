"""Map RAGFlow-style raw block dicts to OpenRag ``DocumentBlock`` (single exit path)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from openrag.parsers.base import DocumentBlock


def to_document_blocks(
    raw: Sequence[Mapping[str, Any]],
    *,
    source: str = "",
) -> list[DocumentBlock]:
    """Convert parser-native rows into ``DocumentBlock`` instances.

    Expected keys per row (all optional except ``text`` for normal blocks):
    ``text``, ``page``, ``offset``, ``bbox``, ``block_type``, ``level``,
    ``layout_type``, ``block_id``, ``char_start``, ``char_end``, ``image``,
    ``table_data``, ``metadata``.

    ``source`` is stored under ``metadata["compat_source"]`` when non-empty.
    """
    blocks: list[DocumentBlock] = []
    for i, item in enumerate(raw):
        bbox = item.get("bbox")
        if bbox is not None:
            if not isinstance(bbox, tuple) or len(bbox) != 4:
                bbox = None
            else:
                bbox = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))

        md = dict(item.get("metadata") or {})
        if source:
            md["compat_source"] = source

        blocks.append(
            DocumentBlock(
                text=str(item.get("text", "")),
                page=int(item.get("page", 1)),
                offset=int(item.get("offset", i)),
                bbox=bbox,
                block_type=str(item.get("block_type", "text")),
                level=int(item.get("level", 0)),
                layout_type=item.get("layout_type"),
                block_id=item.get("block_id"),
                char_start=item.get("char_start"),
                char_end=item.get("char_end"),
                image=item.get("image"),
                table_data=item.get("table_data"),
                metadata=md or None,
            )
        )
    return blocks
