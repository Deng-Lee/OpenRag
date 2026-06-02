"""Conservative document sectionizers for semantic profile chunking."""

from __future__ import annotations

import re
from typing import Any

from openrag.chunking.document_structure import (
    DocumentItem,
    DocumentStructure,
    StructureNode,
)
from openrag.chunking.document_type import normalize_document_type
from openrag.parsers.base import DocumentBlock


def build_document_structure(
    blocks: list[DocumentBlock],
    document_type: str,
) -> DocumentStructure:
    normalized = normalize_document_type(document_type)
    if normalized == "manual":
        return _build_manual_structure(blocks)
    if normalized == "laws":
        return _build_laws_structure(blocks)
    return _build_heading_stack_structure(blocks, assign_sec_id=False)


def _make_items(blocks: list[DocumentBlock]) -> list[DocumentItem]:
    items: list[DocumentItem] = []
    for idx, block in enumerate(blocks):
        text = (block.text or "").strip()
        block_type = (block.block_type or "text").lower()
        items.append(
            DocumentItem(
                item_id=f"item-{idx}",
                block=block,
                text=text,
                block_type=block_type,
                level=block.level or 0,
                page=block.page,
                bbox=block.bbox,
                metadata=dict(block.metadata or {}),
            )
        )
    return items


def _new_node(
    nodes: dict[str, StructureNode],
    node_type: str,
    title: str | None,
    level: int,
    parent_id: str,
    metadata: dict[str, Any] | None = None,
) -> StructureNode:
    node_id = f"node-{len(nodes)}"
    node = StructureNode(
        node_id=node_id,
        node_type=node_type,
        title=title,
        level=level,
        parent_id=parent_id,
        metadata=dict(metadata or {}),
    )
    nodes[node_id] = node
    nodes[parent_id].children.append(node_id)
    return node


def _is_parser_heading(item: DocumentItem) -> bool:
    return item.level > 0 or item.block_type in {"heading", "title"}


def _build_heading_stack_structure(
    blocks: list[DocumentBlock],
    *,
    assign_sec_id: bool,
) -> DocumentStructure:
    items = _make_items(blocks)
    root_id = "root"
    nodes: dict[str, StructureNode] = {
        root_id: StructureNode(root_id, "root", level=0)
    }
    item_to_node: dict[str, str] = {}
    stack: list[tuple[int, str]] = [(0, root_id)]

    for item in items:
        if not item.text:
            continue
        if _is_parser_heading(item):
            level = max(1, item.level)
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            node = _new_node(nodes, "section", item.text, level, stack[-1][1])
            node.metadata["heading_item_id"] = item.item_id
            if assign_sec_id:
                node.metadata["sec_id"] = node.node_id
                item.metadata["sec_id"] = node.node_id
            item_to_node[item.item_id] = node.node_id
            stack.append((level, node.node_id))
            continue

        current_id = stack[-1][1]
        nodes[current_id].item_ids.append(item.item_id)
        item_to_node[item.item_id] = current_id
        if assign_sec_id and current_id != root_id:
            sec_id = nodes[current_id].metadata.setdefault("sec_id", current_id)
            item.metadata["sec_id"] = sec_id

    return DocumentStructure(items, nodes, root_id, item_to_node)


def _build_manual_structure(blocks: list[DocumentBlock]) -> DocumentStructure:
    if any(_is_parser_heading(item) for item in _make_items(blocks)):
        return _build_heading_stack_structure(blocks, assign_sec_id=True)
    return _build_manual_rule_structure(blocks)


_CHINESE_SECTION_RE = re.compile(r"^第[一二三四五六七八九十百千万0-9]+[章节][、\s]?.+")
_NUMERIC_TITLE_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:[、.]\s+|\s+).+")
_STEP_RE = re.compile(r"^(步骤|第\s*\d+\s*步|Step\s*\d+)", re.IGNORECASE)


def _manual_heading_level(text: str) -> int | None:
    if not text or _STEP_RE.match(text):
        return None
    if _CHINESE_SECTION_RE.match(text):
        return 1 if "章" in text[:6] else 2
    if re.match(r"^(Q|问)[:：]", text):
        return 2
    if len(text) <= 30 and text.endswith((":", "：")):
        return 1
    numeric = _NUMERIC_TITLE_RE.match(text)
    if numeric and len(text) <= 60:
        return min(3, numeric.group(1).count(".") + 1)
    return None


def _build_manual_rule_structure(blocks: list[DocumentBlock]) -> DocumentStructure:
    items = _make_items(blocks)
    root_id = "root"
    nodes: dict[str, StructureNode] = {
        root_id: StructureNode(root_id, "root", level=0)
    }
    item_to_node: dict[str, str] = {}
    stack: list[tuple[int, str]] = [(0, root_id)]

    for item in items:
        if not item.text:
            continue
        level = _manual_heading_level(item.text)
        if level is not None:
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            node = _new_node(
                nodes,
                "section",
                item.text,
                level,
                stack[-1][1],
                {"heading_item_id": item.item_id},
            )
            node.metadata["sec_id"] = node.node_id
            item.metadata["sec_id"] = node.node_id
            item_to_node[item.item_id] = node.node_id
            stack.append((level, node.node_id))
            continue

        current_id = stack[-1][1]
        nodes[current_id].item_ids.append(item.item_id)
        item_to_node[item.item_id] = current_id
        if current_id != root_id:
            sec_id = nodes[current_id].metadata.setdefault("sec_id", current_id)
            item.metadata["sec_id"] = sec_id

    return DocumentStructure(items, nodes, root_id, item_to_node)


_LAW_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千万0-9]+章.*")
_LAW_SECTION_RE = re.compile(r"^第[一二三四五六七八九十百千万0-9]+节.*")
_LAW_ARTICLE_RE = re.compile(r"^第[一二三四五六七八九十百千万0-9]+条.*")
_LAW_CLAUSE_RE = re.compile(r"^[（(][一二三四五六七八九十百千万0-9]+[）)].*")
_LAW_NUMERIC_RE = re.compile(r"^(\d+(?:\.\d+){0,2})(?:[、.]\s+|\s+).+")


def _is_simple_toc_line(text: str) -> bool:
    if text in {"目录", "目 录"}:
        return True
    return bool(re.search(r"\.{3,}\s*\d+$", text))


def _law_heading(text: str) -> tuple[str, int] | None:
    if text.startswith("#"):
        level = len(text) - len(text.lstrip("#"))
        return ("section", max(1, min(level, 2)))
    if _LAW_CHAPTER_RE.match(text):
        return ("section", 1)
    if _LAW_SECTION_RE.match(text):
        return ("section", 2)
    if _LAW_ARTICLE_RE.match(text):
        return ("article", 3)
    numeric = _LAW_NUMERIC_RE.match(text)
    if numeric:
        depth = numeric.group(1).count(".") + 1
        return ("section", 1) if depth == 1 else ("article", min(3, depth))
    return None


def _build_laws_structure(blocks: list[DocumentBlock]) -> DocumentStructure:
    items = _make_items(blocks)
    root_id = "root"
    nodes: dict[str, StructureNode] = {
        root_id: StructureNode(root_id, "root", level=0)
    }
    item_to_node: dict[str, str] = {}
    stack: list[tuple[int, str]] = [(0, root_id)]
    current_article_id: str | None = None

    for item in items:
        if not item.text or _is_simple_toc_line(item.text):
            continue

        heading = _law_heading(item.text)
        if heading:
            node_type, level = heading
            while len(stack) > 1 and stack[-1][0] >= level:
                popped = stack.pop()
                if popped[1] == current_article_id:
                    current_article_id = None
            node = _new_node(
                nodes,
                node_type,
                item.text,
                level,
                stack[-1][1],
                {"heading_item_id": item.item_id},
            )
            if node_type == "article":
                node.item_ids.append(item.item_id)
                current_article_id = node.node_id
            item.metadata["sec_id"] = node.node_id
            item_to_node[item.item_id] = node.node_id
            stack.append((level, node.node_id))
            continue

        target_id = current_article_id or stack[-1][1]
        if _LAW_CLAUSE_RE.match(item.text) and current_article_id:
            target_id = current_article_id
        nodes[target_id].item_ids.append(item.item_id)
        item_to_node[item.item_id] = target_id
        item.metadata["sec_id"] = target_id

    return DocumentStructure(items, nodes, root_id, item_to_node)
