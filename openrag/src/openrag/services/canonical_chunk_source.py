"""Build canonical text used as the source coordinate space for chunks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from openrag.parsers.base import DocumentBlock


CANONICAL_CHUNK_SOURCE_VERSION = "chunk_source_v1"

_SUPPORTED_CANONICAL_PARSERS = {
    "MarkdownParserAdapter",
    "TxtParserAdapter",
    "DocxParserAdapter",
}


@dataclass(frozen=True)
class CanonicalChunkSource:
    text: str
    blocks: list[DocumentBlock]


def supports_canonical_chunk_source(parser_name: str) -> bool:
    return parser_name in _SUPPORTED_CANONICAL_PARSERS


def canonical_source_metadata() -> dict:
    return {
        "version": CANONICAL_CHUNK_SOURCE_VERSION,
        "applies_to": ["md", "txt", "docx"],
        "block_text": "strip",
        "joiner": "\\n",
        "preserve_parser_block_order": True,
        "preserve_parser_table_position": True,
        "trailing_newline": False,
    }


def build_canonical_chunk_source(
    blocks: Iterable[DocumentBlock],
) -> CanonicalChunkSource:
    text_parts: list[str] = []
    normalized_blocks: list[DocumentBlock] = []
    cursor = 0

    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue

        if text_parts:
            cursor += 1

        start = cursor
        end = start + len(text)
        text_parts.append(text)
        normalized_blocks.append(
            replace(
                block,
                text=text,
                offset=start,
                char_start=start,
                char_end=end,
            )
        )
        cursor = end

    return CanonicalChunkSource(
        text="\n".join(text_parts),
        blocks=normalized_blocks,
    )
