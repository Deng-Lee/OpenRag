"""Lightweight document structure model for profile chunking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openrag.parsers.base import DocumentBlock


@dataclass
class DocumentItem:
    item_id: str
    block: DocumentBlock
    text: str
    block_type: str
    level: int
    page: int
    bbox: tuple[float, float, float, float] | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StructureNode:
    node_id: str
    node_type: str
    title: str | None = None
    level: int = 0
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkUnit:
    text: str
    unit_type: str
    source_items: list[DocumentItem]
    source_blocks: list[DocumentBlock]
    section_path: list[str]
    section_level: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentStructure:
    def __init__(
        self,
        items: list[DocumentItem],
        nodes: dict[str, StructureNode],
        root_id: str,
        item_to_node: dict[str, str],
    ) -> None:
        self.items = items
        self.nodes = nodes
        self.root_id = root_id
        self.item_to_node = item_to_node
        self._items_by_id = {item.item_id: item for item in items}

    def node_path(self, node_id: str) -> list[str]:
        path: list[str] = []
        current = self.nodes.get(node_id)
        while current is not None:
            if current.title:
                path.append(current.title)
            parent_id = current.parent_id
            current = self.nodes.get(parent_id) if parent_id else None
        return list(reversed(path))

    def node_source_blocks(self, node_id: str) -> list[DocumentBlock]:
        return [item.block for item in self.items_for_node(node_id)]

    def items_for_node(self, node_id: str) -> list[DocumentItem]:
        node = self.nodes[node_id]
        return [
            self._items_by_id[item_id]
            for item_id in node.item_ids
            if item_id in self._items_by_id
        ]

    def item_by_id(self, item_id: str) -> DocumentItem | None:
        return self._items_by_id.get(item_id)
