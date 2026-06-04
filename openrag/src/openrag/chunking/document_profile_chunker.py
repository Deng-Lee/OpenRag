"""Build document-type aware semantic profile items."""

from __future__ import annotations

from typing import Any

from openrag.chunking.document_sectionizer import build_document_structure
from openrag.chunking.document_structure import ChunkUnit, DocumentItem, DocumentStructure
from openrag.chunking.document_type import normalize_document_type
from openrag.parsers.base import DocumentBlock

try:
    from common.token_utils import num_tokens_from_string
except ImportError:
    def num_tokens_from_string(text: str) -> int:
        return max(1, len(text) // 4)


def build_profile_items(
    blocks: list[DocumentBlock],
    document_type: str,
    chunk_size: int,
    min_chunk_tokens: int,
) -> list[dict[str, Any]] | None:
    _ = chunk_size
    _ = min_chunk_tokens
    normalized = normalize_document_type(document_type)
    if normalized == "general":
        return None
    structure = build_document_structure(blocks, normalized)
    if normalized == "manual":
        return _manual_profile_items(structure)
    if normalized == "laws":
        return _laws_profile_items(structure)
    return None


def _heading_item(structure: DocumentStructure, node_id: str) -> DocumentItem | None:
    heading_item_id = structure.nodes[node_id].metadata.get("heading_item_id")
    if not heading_item_id:
        return None
    return structure.item_by_id(str(heading_item_id))


def _unique_items(items: list[DocumentItem]) -> list[DocumentItem]:
    seen: set[str] = set()
    unique: list[DocumentItem] = []
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        unique.append(item)
    return unique


def _unit_for_node(
    structure: DocumentStructure,
    node_id: str,
    document_type: str,
    unit_type: str | None = None,
) -> ChunkUnit | None:
    node = structure.nodes[node_id]
    source_items: list[DocumentItem] = []
    title_item = _heading_item(structure, node_id)
    if title_item is not None:
        source_items.append(title_item)
    source_items.extend(structure.items_for_node(node_id))
    source_items = _unique_items(source_items)
    text_parts = [item.text for item in source_items if item.text]
    if not text_parts:
        return None

    block_types = [
        item.block_type if item.block_type in {"table", "image"} else "text"
        for item in source_items
    ]
    normalized_unit_type = unit_type or (
        block_types[0] if len(set(block_types)) == 1 else "text"
    )
    section_path = structure.node_path(node_id)
    parent_heading = section_path[-2] if len(section_path) >= 2 else None
    sec_id = node.metadata.get("sec_id") or node.node_id
    metadata: dict[str, Any] = {
        "document_type": document_type,
        "section_path": section_path,
        "section_level": node.level,
        "structure_node_type": node.node_type,
        "parent_heading": parent_heading,
        "sec_id": sec_id,
        "contains_block_types": sorted(set(block_types)),
    }
    return ChunkUnit(
        text="\n".join(text_parts),
        unit_type=normalized_unit_type,
        source_items=source_items,
        source_blocks=[item.block for item in source_items],
        section_path=section_path,
        section_level=node.level,
        metadata=metadata,
    )


def _root_unit(structure: DocumentStructure, document_type: str) -> ChunkUnit | None:
    root = structure.nodes[structure.root_id]
    source_items = structure.items_for_node(root.node_id)
    text_parts = [item.text for item in source_items if item.text]
    if not text_parts:
        return None
    block_types = [
        item.block_type if item.block_type in {"table", "image"} else "text"
        for item in source_items
    ]
    metadata = {
        "document_type": document_type,
        "section_path": [],
        "section_level": 0,
        "structure_node_type": "root",
        "parent_heading": None,
        "sec_id": root.node_id,
        "contains_block_types": sorted(set(block_types)),
    }
    return ChunkUnit(
        text="\n".join(text_parts),
        unit_type="text",
        source_items=source_items,
        source_blocks=[item.block for item in source_items],
        section_path=[],
        metadata=metadata,
    )


def _item_from_unit(unit: ChunkUnit) -> dict[str, Any]:
    metadata = dict(unit.metadata)
    ck_type = unit.unit_type if unit.unit_type in {"table", "image"} else "text"
    item = {
        "text": unit.text,
        "ck_type": ck_type,
        "image": next((b.image for b in unit.source_blocks if b.image is not None), None),
        "source_blocks": unit.source_blocks,
        "section_path": unit.section_path,
        "document_type": metadata.get("document_type"),
        "sec_id": metadata.get("sec_id"),
        "metadata": metadata,
        "tk_nums": num_tokens_from_string(unit.text),
    }
    item.update(metadata)
    return item


def _merge_item_into_previous(previous: dict[str, Any], item: dict[str, Any]) -> None:
    previous["text"] = "\n".join(
        part for part in [previous.get("text") or "", item.get("text") or ""] if part
    )
    previous["source_blocks"] = list(previous.get("source_blocks") or []) + list(
        item.get("source_blocks") or []
    )
    previous["tk_nums"] = num_tokens_from_string(previous["text"])
    contains = set(previous.get("contains_block_types") or [])
    contains.update(item.get("contains_block_types") or [])
    previous["contains_block_types"] = sorted(contains)
    metadata = dict(previous.get("metadata") or {})
    metadata["contains_block_types"] = previous["contains_block_types"]
    previous["metadata"] = metadata
    if previous.get("image") is None and item.get("image") is not None:
        previous["image"] = item["image"]


def _manual_profile_items(structure: DocumentStructure) -> list[dict[str, Any]] | None:
    if not any(node_id != structure.root_id for node_id in structure.nodes):
        return None

    units: list[ChunkUnit] = []
    root_unit = _root_unit(structure, "manual")
    if root_unit is not None:
        units.append(root_unit)
    for node_id, node in structure.nodes.items():
        if node_id == structure.root_id:
            continue
        unit = _unit_for_node(structure, node_id, "manual")
        if unit is not None:
            units.append(unit)

    merged: list[dict[str, Any]] = []
    for unit in units:
        item = _item_from_unit(unit)
        if merged:
            prev = merged[-1]
            prev_tokens = int(prev.get("tk_nums") or num_tokens_from_string(prev.get("text") or ""))
            same_sec = prev.get("sec_id") == item.get("sec_id")
            is_media = item.get("ck_type") in {"table", "image"}
            if prev_tokens < 32 or (prev_tokens < 1024 and same_sec) or (
                is_media and prev_tokens < 1024
            ):
                _merge_item_into_previous(prev, item)
                continue
        merged.append(item)
    return merged or None


def _laws_profile_items(structure: DocumentStructure) -> list[dict[str, Any]] | None:
    items: list[dict[str, Any]] = []
    for node_id, node in structure.nodes.items():
        if node_id == structure.root_id:
            continue
        if node.node_type != "article" and not node.item_ids:
            continue
        unit = _unit_for_node(
            structure,
            node_id,
            "laws",
            "article" if node.node_type == "article" else node.node_type,
        )
        if unit is None:
            continue
        item = _item_from_unit(unit)
        item["structure_node_type"] = node.node_type
        item["law_target_level"] = min(2, node.level)
        metadata = dict(item.get("metadata") or {})
        metadata["structure_node_type"] = node.node_type
        metadata["law_target_level"] = item["law_target_level"]
        item["metadata"] = metadata
        items.append(item)
    return items or None
