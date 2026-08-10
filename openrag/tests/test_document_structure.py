"""Tests for document structure sectionizers."""

from src.openrag.chunking.document_sectionizer import build_document_structure
from src.openrag.parsers.base import DocumentBlock


def _block(text, block_type="text", level=0, offset=0, bbox=None):
    return DocumentBlock(
        text=text,
        page=1,
        offset=offset,
        bbox=bbox,
        block_type=block_type,
        level=level,
        block_id=f"block-{offset}",
    )


def test_general_heading_stack_keeps_section_hierarchy():
    blocks = [
        _block("H1 A", "heading", 1, 0),
        _block("A body", "text", 0, 10),
        _block("H2 A.1", "heading", 2, 20),
        _block("A.1 body", "text", 0, 30),
        _block("H1 B", "heading", 1, 40),
    ]

    structure = build_document_structure(blocks, "general")

    h1_a = structure.nodes["node-1"]
    h2_a1 = structure.nodes["node-2"]
    h1_b = structure.nodes["node-3"]
    assert structure.node_path(h2_a1.node_id) == ["H1 A", "H2 A.1"]
    assert structure.items_for_node(h1_a.node_id)[0].text == "A body"
    assert structure.items_for_node(h2_a1.node_id)[0].text == "A.1 body"
    assert h1_b.parent_id == structure.root_id


def test_general_without_headings_keeps_root_order():
    blocks = [
        _block("first", offset=0),
        _block("table row", "table", offset=10),
        _block("image caption", "image", offset=20),
    ]

    structure = build_document_structure(blocks, "general")

    assert [item.text for item in structure.items_for_node(structure.root_id)] == [
        "first",
        "table row",
        "image caption",
    ]


def test_level_zero_title_candidate_is_not_promoted_to_h1():
    blocks = [
        _block("封面标题", "title", level=0, offset=0),
        _block("正文", offset=10),
    ]

    structure = build_document_structure(blocks, "general")

    assert list(structure.nodes) == [structure.root_id]
    assert [item.text for item in structure.items_for_node(structure.root_id)] == [
        "封面标题",
        "正文",
    ]


def test_manual_rule_headings_keep_steps_and_table_in_section():
    blocks = [
        _block("安装流程：", offset=0),
        _block("说明：按顺序执行。", offset=10),
        _block("步骤 1：打开控制台。", offset=20),
        _block("参数 | 说明", "table", offset=30),
    ]

    structure = build_document_structure(blocks, "manual")

    section = structure.nodes["node-1"]
    items = structure.items_for_node(section.node_id)
    assert section.title == "安装流程："
    assert [item.text for item in items] == [
        "说明：按顺序执行。",
        "步骤 1：打开控制台。",
        "参数 | 说明",
    ]
    assert section.metadata["sec_id"]
    assert all(item.metadata.get("sec_id") == section.metadata["sec_id"] for item in items)


def test_laws_groups_article_with_clauses_and_keeps_next_article_separate():
    blocks = [
        _block("第一章 总则", offset=0),
        _block("第一条 为了规范事项，制定本法。", offset=10),
        _block("（一）适用范围。", offset=20),
        _block("第二条 本法适用于相关活动。", offset=30),
    ]

    structure = build_document_structure(blocks, "laws")

    articles = [
        node for node in structure.nodes.values() if node.node_type == "article"
    ]
    assert len(articles) == 2
    assert structure.node_path(articles[0].node_id) == ["第一章 总则", "第一条 为了规范事项，制定本法。"]
    assert [item.text for item in structure.items_for_node(articles[0].node_id)] == [
        "第一条 为了规范事项，制定本法。",
        "（一）适用范围。",
    ]
    assert [item.text for item in structure.items_for_node(articles[1].node_id)] == [
        "第二条 本法适用于相关活动。",
    ]
