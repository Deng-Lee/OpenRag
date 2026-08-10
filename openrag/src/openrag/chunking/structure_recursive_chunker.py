"""Structure-aware recursive chunking for general PDF documents."""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid

from openrag.chunking.chunk_models import Chunk
from openrag.chunking.ragflow_core.metadata import ragflow_add_positions_fields
from openrag.parsers.base import DocumentBlock

try:
    from common.token_utils import num_tokens_from_string
except ImportError:
    def num_tokens_from_string(text: str) -> int:
        return max(1, len(text) // 4)


_SEPARATORS = (
    re.compile(r"\n\s*\n+"),
    re.compile(r"\n+"),
    re.compile(r"(?<=[。！？.!?])"),
    re.compile(r"(?<=[；;])"),
    re.compile(r"(?<=[，,])"),
    re.compile(r"\s+"),
)


@dataclass(frozen=True)
class _Region:
    region_id: str
    title: str | None
    blocks: list[DocumentBlock]
    structure_quality: str


@dataclass(frozen=True)
class _Fragment:
    text: str
    block: DocumentBlock
    local_start: int
    local_end: int
    soft_boundary_before: bool = False
    atomic: bool = False


def chunk_general_structured_recursive(
    blocks: list[DocumentBlock],
    *,
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_tokens: int,
) -> list[Chunk]:
    """Chunk ordered blocks without crossing a trusted H1 region."""
    if not blocks:
        return []
    chunks: list[Chunk] = []
    for region in _build_regions(blocks):
        fragments = [
            fragment
            for block in region.blocks
            for fragment in _block_fragments(block, chunk_size)
        ]
        groups = _pack_fragments(fragments, chunk_size)
        groups = _merge_short_tail(groups, chunk_size, min_chunk_tokens)
        groups = _apply_overlap(groups, chunk_size, chunk_overlap)
        chunks.extend(
            _make_chunk(group, region, chunk_size)
            for group in groups
            if group
        )
    return chunks


def _build_regions(blocks: list[DocumentBlock]) -> list[_Region]:
    h1_indices = [
        index
        for index, block in enumerate(blocks)
        if block.level == 1 and bool((block.metadata or {}).get("hard_boundary"))
    ]
    if not h1_indices:
        return [_Region("document-root", None, blocks, "no_trusted_h1")]

    regions: list[_Region] = []
    first_h1 = h1_indices[0]
    if first_h1 > 0:
        regions.append(_Region("preamble", None, blocks[:first_h1], "full"))
    for position, start in enumerate(h1_indices):
        end = h1_indices[position + 1] if position + 1 < len(h1_indices) else len(blocks)
        heading = blocks[start]
        regions.append(
            _Region(
                heading.block_id or f"h1-{start}",
                heading.text,
                blocks[start:end],
                "full",
            )
        )
    return regions


def _block_fragments(block: DocumentBlock, limit: int) -> list[_Fragment]:
    text = block.text or ""
    if not text:
        return []
    block_type = (block.block_type or "text").lower()
    atomic = block_type in {"table", "image", "figure"}
    soft_boundary = block.level > 1 and block_type == "heading"
    if atomic or num_tokens_from_string(text) <= limit:
        return [
            _Fragment(
                text,
                block,
                0,
                len(text),
                soft_boundary_before=soft_boundary,
                atomic=atomic,
            )
        ]

    spans = _recursive_split(text, 0, limit, 0)
    return [
        _Fragment(
            piece,
            block,
            start,
            end,
            soft_boundary_before=soft_boundary and index == 0,
        )
        for index, (piece, start, end) in enumerate(spans)
        if piece
    ]


def _recursive_split(
    text: str,
    base_start: int,
    limit: int,
    separator_index: int,
) -> list[tuple[str, int, int]]:
    if num_tokens_from_string(text) <= limit:
        return [(text, base_start, base_start + len(text))]
    if separator_index >= len(_SEPARATORS):
        return _fixed_prefix_spans(text, base_start, limit)

    pieces = _split_with_offsets(text, _SEPARATORS[separator_index])
    if len(pieces) <= 1:
        return _recursive_split(text, base_start, limit, separator_index + 1)

    result: list[tuple[str, int, int]] = []
    buffer_text = ""
    buffer_start = 0
    buffer_end = 0
    for piece, start, end in pieces:
        combined = buffer_text + piece
        if buffer_text and num_tokens_from_string(combined) > limit:
            result.append(
                (buffer_text, base_start + buffer_start, base_start + buffer_end)
            )
            buffer_text = ""

        if num_tokens_from_string(piece) > limit:
            if buffer_text:
                result.append(
                    (buffer_text, base_start + buffer_start, base_start + buffer_end)
                )
                buffer_text = ""
            result.extend(
                _recursive_split(
                    piece,
                    base_start + start,
                    limit,
                    separator_index + 1,
                )
            )
            continue

        if not buffer_text:
            buffer_start = start
            buffer_text = piece
        else:
            buffer_text += piece
        buffer_end = end

    if buffer_text:
        result.append((buffer_text, base_start + buffer_start, base_start + buffer_end))
    return result


def _split_with_offsets(
    text: str,
    pattern: re.Pattern[str],
) -> list[tuple[str, int, int]]:
    boundaries = [match.end() for match in pattern.finditer(text)]
    boundaries = [boundary for boundary in boundaries if 0 < boundary < len(text)]
    if not boundaries:
        return [(text, 0, len(text))]
    pieces: list[tuple[str, int, int]] = []
    start = 0
    for end in boundaries:
        if end > start:
            pieces.append((text[start:end], start, end))
            start = end
    if start < len(text):
        pieces.append((text[start:], start, len(text)))
    return pieces


def _fixed_prefix_spans(
    text: str,
    base_start: int,
    limit: int,
) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        low = start + 1
        high = len(text)
        best = low
        while low <= high:
            middle = (low + high) // 2
            if num_tokens_from_string(text[start:middle]) <= limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best <= start:
            best = start + 1
        spans.append((text[start:best], base_start + start, base_start + best))
        start = best
    return spans


def _pack_fragments(
    fragments: list[_Fragment],
    limit: int,
) -> list[list[_Fragment]]:
    groups: list[list[_Fragment]] = []
    current: list[_Fragment] = []
    index = 0
    while index < len(fragments):
        fragment = fragments[index]
        if not current or _group_tokens(current + [fragment]) <= limit:
            current.append(fragment)
            index += 1
            continue

        soft_index = _last_soft_boundary(current)
        if soft_index is not None:
            groups.append(current[:soft_index])
            current = current[soft_index:]
            continue

        groups.append(current)
        current = []
    if current:
        groups.append(current)
    return groups


def _last_soft_boundary(group: list[_Fragment]) -> int | None:
    for index in range(len(group) - 1, 0, -1):
        if group[index].soft_boundary_before:
            return index
    return None


def _merge_short_tail(
    groups: list[list[_Fragment]],
    limit: int,
    minimum: int,
) -> list[list[_Fragment]]:
    if minimum <= 0 or len(groups) < 2:
        return groups
    if _group_tokens(groups[-1]) >= minimum:
        return groups
    merged = groups[-2] + groups[-1]
    if _group_tokens(merged) > limit:
        return groups
    return groups[:-2] + [merged]


def _apply_overlap(
    groups: list[list[_Fragment]],
    limit: int,
    overlap: int,
) -> list[list[_Fragment]]:
    if overlap <= 0 or len(groups) < 2:
        return groups
    result = [groups[0]]
    for group in groups[1:]:
        tail: list[_Fragment] = []
        for fragment in reversed(result[-1]):
            if fragment.atomic or fragment.block.level > 0:
                break
            candidate = [fragment] + tail
            if _group_tokens(candidate) > overlap:
                break
            if _group_tokens(candidate + group) > limit:
                break
            tail = candidate
        result.append(tail + group)
    return result


def _group_text(group: list[_Fragment]) -> str:
    text = ""
    previous: _Fragment | None = None
    for fragment in group:
        if not text:
            text = fragment.text
        elif (
            previous is not None
            and previous.block is fragment.block
            and previous.local_end == fragment.local_start
        ):
            text += fragment.text
        else:
            text += "\n" + fragment.text
        previous = fragment
    return text


def _group_tokens(group: list[_Fragment]) -> int:
    return num_tokens_from_string(_group_text(group))


def _make_chunk(group: list[_Fragment], region: _Region, limit: int) -> Chunk:
    text = _group_text(group)
    blocks = _unique_blocks(group)
    block_ids = [block.block_id for block in blocks if block.block_id]
    pages = sorted({block.page for block in blocks})
    heading_blocks = [block for block in blocks if block.level > 0]
    contained_paths = [
        list((block.metadata or {}).get("heading_path") or [])
        for block in heading_blocks
    ]
    contained_paths = [path for path in contained_paths if path]
    source_start = _fragment_absolute_start(group[0])
    source_end = _fragment_absolute_end(group[-1])
    block_types = {(block.block_type or "text").lower() for block in blocks}
    output_type = (
        next(iter(block_types))
        if len(block_types) == 1 and next(iter(block_types)) in {"table", "image"}
        else "text"
    )
    metadata = {
        "document_type": "general",
        "chunk_strategy": "structured_recursive_v1",
        "h1_region_id": region.region_id,
        "h1_title": region.title,
        "structure_quality": region.structure_quality,
        "source_block_ids": block_ids,
        "page_range": [pages[0], pages[-1]] if pages else [],
        "contained_heading_ids": [
            block.block_id for block in heading_blocks if block.block_id
        ],
        "contained_heading_paths": contained_paths,
        "active_heading_path": list(
            (blocks[0].metadata or {}).get("heading_path") or []
        ),
        "hard_boundary_respected": True,
    }
    if region.structure_quality == "no_trusted_h1":
        metadata["fallback_reason"] = "no_trusted_h1"
    if num_tokens_from_string(text) > limit:
        metadata["over_limit_reason"] = (
            "atomic_block" if any(fragment.atomic for fragment in group) else "unknown"
        )
    metadata.update(
        ragflow_add_positions_fields(
            [
                (
                    block.page - 1,
                    block.bbox[0],
                    block.bbox[2],
                    block.bbox[1],
                    block.bbox[3],
                )
                for block in blocks
                if block.bbox is not None
            ]
        )
    )

    bbox = _same_page_bbox(blocks)
    levels = [block.level for block in heading_blocks]
    return Chunk(
        text=text,
        chunk_id=str(uuid.uuid4()),
        page=pages[0] if pages else 0,
        start_offset=source_start,
        end_offset=source_end,
        bbox=bbox,
        source_block_id=block_ids[0] if block_ids else None,
        source_char_start=source_start,
        source_char_end=source_end,
        level=min(levels) if levels else 0,
        block_type=output_type,
        metadata=metadata,
    )


def _unique_blocks(group: list[_Fragment]) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    seen: set[int] = set()
    for fragment in group:
        identity = id(fragment.block)
        if identity in seen:
            continue
        seen.add(identity)
        blocks.append(fragment.block)
    return blocks


def _fragment_absolute_start(fragment: _Fragment) -> int:
    base = (
        fragment.block.char_start
        if fragment.block.char_start is not None
        else fragment.block.offset
    )
    return int(base + fragment.local_start)


def _fragment_absolute_end(fragment: _Fragment) -> int:
    base = (
        fragment.block.char_start
        if fragment.block.char_start is not None
        else fragment.block.offset
    )
    return int(base + fragment.local_end)


def _same_page_bbox(
    blocks: list[DocumentBlock],
) -> tuple[float, float, float, float] | None:
    if not blocks or len({block.page for block in blocks}) != 1:
        return None
    boxes = [block.bbox for block in blocks if block.bbox is not None]
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
