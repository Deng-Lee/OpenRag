from openrag.parsers.base import DocumentBlock
from pypdf import PdfWriter

from openrag.parsers.pdf_heading_hierarchy import (
    PdfHeadingHierarchyResolver,
    extract_pdf_outlines,
)


def _block(text, *, layout_type="title", page=1, offset=0):
    return DocumentBlock(
        text=text,
        page=page,
        offset=offset,
        block_type="text",
        level=0,
        layout_type=layout_type,
        block_id=f"block-{offset}",
        metadata={"compat_source": "pdf", "structured_pdf": True},
    )


def test_outline_depth_maps_to_heading_level_and_parent_path():
    blocks = [
        _block("第一章 总则", offset=0),
        _block("正文", layout_type="text", offset=10),
        _block("1.1 范围", offset=20),
        _block("范围正文", layout_type="text", offset=30),
    ]

    resolved = PdfHeadingHierarchyResolver().resolve(
        blocks,
        [("第一章 总则", 0, 1), ("1.1 范围", 1, 1)],
    )

    assert (resolved[0].block_type, resolved[0].level) == ("heading", 1)
    assert resolved[0].metadata["hard_boundary"] is True
    assert (resolved[2].block_type, resolved[2].level) == ("heading", 2)
    assert resolved[2].metadata["parent_heading_id"] == resolved[0].block_id
    assert resolved[3].metadata["heading_path"] == ["第一章 总则", "1.1 范围"]


def test_strong_chapter_numbering_forms_h1_without_outline():
    blocks = [
        _block("第一章 总则", offset=0),
        _block("正文", layout_type="text", offset=10),
        _block("第二章 安全要求", page=2, offset=20),
    ]

    resolved = PdfHeadingHierarchyResolver().resolve(blocks, [])

    assert [block.level for block in resolved] == [1, 0, 1]
    assert resolved[0].metadata["heading_sources"] == ["numbering"]
    assert resolved[2].metadata["hard_boundary"] is True


def test_layout_title_without_level_evidence_stays_non_heading():
    block = _block("概述", offset=0)

    resolved = PdfHeadingHierarchyResolver().resolve([block], [])

    assert resolved[0].block_type == "text"
    assert resolved[0].level == 0
    assert resolved[0].metadata["heading_candidate"] is True
    assert resolved[0].metadata["hard_boundary"] is False


def test_document_title_is_not_a_hard_boundary():
    block = _block("OpenRAG 技术白皮书", offset=0)

    resolved = PdfHeadingHierarchyResolver().resolve([block], [])

    assert resolved[0].metadata["heading_role"] == "document_title"
    assert resolved[0].metadata["hard_boundary"] is False


def test_style_can_inherit_level_from_anchor_but_not_create_hard_boundary():
    anchor = _block("1.1 范围", offset=0)
    anchor.metadata.update(
        {
            "font_size_median": 18.0,
            "font_name_mode": "BoldFont",
            "style_source": "pdf_chars",
        }
    )
    inferred = _block("适用对象", offset=10)
    inferred.metadata.update(
        {
            "font_size_median": 18.2,
            "font_name_mode": "BoldFont",
            "style_source": "pdf_chars",
        }
    )

    resolved = PdfHeadingHierarchyResolver().resolve(
        [anchor, inferred],
        [("1.1 范围", 1, 1)],
    )

    assert resolved[1].level == 2
    assert resolved[1].metadata["heading_sources"] == ["anchored_style"]
    assert resolved[1].metadata["hard_boundary"] is False


def test_paddle_document_title_is_never_promoted_by_numbering():
    block = _block("第一章 项目白皮书", offset=0)
    block.metadata.update(
        {
            "compat_source": "paddleocr",
            "parser_backend": "paddleocr",
            "ocr_label": "doc_title",
            "heading_role": "document_title",
            "paddle_markdown_level_raw": 1,
            "paddle_markdown_level": 0,
        }
    )

    resolved = PdfHeadingHierarchyResolver().resolve([block], [])

    assert resolved[0].block_type == "text"
    assert resolved[0].level == 0
    assert resolved[0].metadata["heading_role"] == "document_title"
    assert resolved[0].metadata["hard_boundary"] is False


def test_paddle_markdown_and_consistent_geometry_can_support_h1():
    first = _block("系统概述", offset=0)
    second = _block("部署说明", page=2, offset=10)
    for block in (first, second):
        block.metadata.update(
            {
                "compat_source": "paddleocr",
                "parser_backend": "paddleocr",
                "ocr_label": "paragraph_title",
                "heading_candidate": True,
                "paddle_markdown_level": 1,
                "layout_score": 0.86,
                "geometry_height_ratio": 0.025,
                "style_source": "paddle_bbox",
            }
        )

    resolved = PdfHeadingHierarchyResolver().resolve([first, second], [])

    assert [block.level for block in resolved] == [1, 1]
    assert all(block.block_type == "heading" for block in resolved)
    assert all(block.metadata["hard_boundary"] is True for block in resolved)
    assert resolved[0].metadata["heading_sources"] == [
        "paddle_markdown",
        "geometry_style",
    ]


def test_extract_pdf_outlines_returns_level_and_page(tmp_path):
    pdf = tmp_path / "outline.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    parent = writer.add_outline_item("第一章", 0)
    writer.add_outline_item("1.1 范围", 0, parent=parent)
    with pdf.open("wb") as file_obj:
        writer.write(file_obj)

    outlines = extract_pdf_outlines(str(pdf))

    assert [(item.text, item.level, item.page) for item in outlines] == [
        ("第一章", 1, 1),
        ("1.1 范围", 2, 1),
    ]
