"""RAGFlow PDF 位置标签：制表符或空格分隔均需可解析（入库 bbox 依赖此）。"""

from openrag.parsers.adapters.pdf_adapter import (
    PDFParserAdapter,
    _extract_pdf_position_tags,
    _page_bbox_from_ragflow_table_positions,
    _plain_line_from_tagged_line,
    _plain_page_bbox_from_tagged_paragraph,
    _tagged_paragraph_to_plain_bbox_and_lines,
    _unpack_ragflow_table_item,
)


def test_space_separated_position_tag_matches_user_payload():
    s = (
        "安装步骤：以下我们以最简单的本地部署、本地访问来演示安装步骤，"
        "不到5分钟就能开始玩龙虾了。@@3 41.7 510.0 614.0 626.3##"
    )
    plain, page, bbox = _plain_page_bbox_from_tagged_paragraph(s)
    assert "@@" not in plain and "##" not in plain
    assert page == 3
    assert bbox == (41.7, 614.0, 510.0, 626.3)


def test_tab_separated_position_tag_still_works():
    s = "段落A@@3\t41.7\t510.0\t614.0\t626.3##\n\n"
    plain, page, bbox = _plain_page_bbox_from_tagged_paragraph(s.strip())
    assert plain == "段落A"
    assert page == 3
    assert bbox == (41.7, 614.0, 510.0, 626.3)


def test_extract_pdf_position_tags_accepts_multiple_lines():
    s = (
        "第一行@@2\t10.0\t110.0\t20.0\t30.0##\n"
        "第二行@@2 12.0 120.0 40.0 55.0##"
    )
    assert _extract_pdf_position_tags(s) == [
        {"page": 2, "x0": 10.0, "x1": 110.0, "top": 20.0, "bottom": 30.0},
        {"page": 2, "x0": 12.0, "x1": 120.0, "top": 40.0, "bottom": 55.0},
    ]


def test_plain_line_from_tagged_line_removes_space_and_tab_tags():
    line = "前半@@1 1.0 2.0 3.0 4.0##后半@@1\t5.0\t6.0\t7.0\t8.0##"
    assert _plain_line_from_tagged_line(line) == "前半后半"


def test_tagged_paragraph_returns_plain_bbox_and_line_positions():
    para = (
        "第一行@@2\t10.0\t110.0\t20.0\t30.0##\n"
        "第二行@@2 12.0 120.0 40.0 55.0##"
    )
    plain, page, bbox, line_positions = _tagged_paragraph_to_plain_bbox_and_lines(
        para
    )
    assert plain == "第一行\n第二行"
    assert page == 2
    assert bbox == (10.0, 20.0, 120.0, 55.0)
    assert line_positions == [
        {
            "page": 2,
            "x0": 10.0,
            "x1": 110.0,
            "top": 20.0,
            "bottom": 30.0,
            "char_start": 0,
            "char_end": 3,
            "text": "第一行",
        },
        {
            "page": 2,
            "x0": 12.0,
            "x1": 120.0,
            "top": 40.0,
            "bottom": 55.0,
            "char_start": 4,
            "char_end": 7,
            "text": "第二行",
        },
    ]


def test_tagged_paragraph_line_positions_can_use_absolute_offsets():
    para = (
        "abc@@2\t10.0\t110.0\t20.0\t30.0##\n"
        "de@@2 12.0 120.0 40.0 55.0##"
    )
    plain, page, bbox, line_positions = _tagged_paragraph_to_plain_bbox_and_lines(
        para,
        abs_start=8,
    )
    assert plain == "abc\nde"
    assert page == 2
    assert bbox == (10.0, 20.0, 120.0, 55.0)
    assert line_positions[0]["char_start"] == 8
    assert line_positions[0]["char_end"] == 11
    assert line_positions[1]["char_start"] == 12
    assert line_positions[1]["char_end"] == 14


def test_tagged_paragraph_plain_text_has_no_bbox_or_line_positions():
    plain, page, bbox, line_positions = _tagged_paragraph_to_plain_bbox_and_lines(
        "第一行\n第二行"
    )
    assert plain == "第一行\n第二行"
    assert page == 1
    assert bbox is None
    assert line_positions == []


def test_pdf_adapter_writes_absolute_line_positions_to_metadata():
    adapter = object.__new__(PDFParserAdapter)
    adapter.ragflow_parser = lambda _path: (
        (
            "abc@@1 1.0 2.0 3.0 4.0##\n"
            "de@@1 5.0 6.0 7.0 8.0##\n\n"
            "xyz@@2 9.0 10.0 11.0 12.0##"
        ),
        [],
    )

    blocks = adapter._parse_ragflow_with_tables("dummy.pdf")

    assert [block.text for block in blocks] == ["abc\nde", "xyz"]
    assert (blocks[0].char_start, blocks[0].char_end) == (0, 6)
    assert (blocks[1].char_start, blocks[1].char_end) == (8, 11)
    assert blocks[0].metadata["line_positions"] == [
        {
            "page": 1,
            "x0": 1.0,
            "x1": 2.0,
            "top": 3.0,
            "bottom": 4.0,
            "char_start": 0,
            "char_end": 3,
            "text": "abc",
        },
        {
            "page": 1,
            "x0": 5.0,
            "x1": 6.0,
            "top": 7.0,
            "bottom": 8.0,
            "char_start": 4,
            "char_end": 6,
            "text": "de",
        },
    ]
    assert blocks[1].metadata["line_positions"] == [
        {
            "page": 2,
            "x0": 9.0,
            "x1": 10.0,
            "top": 11.0,
            "bottom": 12.0,
            "char_start": 8,
            "char_end": 11,
            "text": "xyz",
        }
    ]


def test_adapter_helpers_remove_tag_and_extract_positions_space_form():
    s = "x@@2 1.0 2.0 3.0 4.0##"
    assert _plain_line_from_tagged_line(s) == "x"
    assert _extract_pdf_position_tags(s) == [
        {"page": 2, "x0": 1.0, "x1": 2.0, "top": 3.0, "bottom": 4.0}
    ]


def test_table_positions_to_page_and_bbox_union():
    """与 RAGFlow cropout 写入的 poss 一致：pn 为 0-based 全文档页下标。"""
    poss = [(1, 10.0, 110.0, 20.0, 80.0), (1, 10.0, 110.0, 90.0, 150.0)]
    page, bbox, per_page = _page_bbox_from_ragflow_table_positions(poss)
    assert page == 2
    assert bbox == (10.0, 20.0, 110.0, 150.0)
    assert per_page == {2: (10.0, 20.0, 110.0, 150.0)}


def test_cross_page_table_bbox_uses_primary_page_only():
    """跨页表格：bbox 应仅基于首页坐标，不能混合不同页的 y 值。"""
    poss = [
        (14, 30.0, 500.0, 600.0, 800.0),  # page 15 (0-based 14): bottom portion
        (15, 30.0, 500.0, 50.0, 400.0),   # page 16 (0-based 15): top portion
    ]
    page, bbox, per_page = _page_bbox_from_ragflow_table_positions(poss)
    assert page == 15
    assert bbox == (30.0, 600.0, 500.0, 800.0), (
        "bbox should only contain page-15 coords, not mixed with page-16"
    )
    assert per_page == {
        15: (30.0, 600.0, 500.0, 800.0),
        16: (30.0, 50.0, 500.0, 400.0),
    }


def test_unpack_ragflow_table_item_with_positions():
    inner = (b"\xff", "a\nb")
    wrapped = (inner, [(0, 1.0, 2.0, 3.0, 4.0)])
    img, data, poss = _unpack_ragflow_table_item(wrapped)
    assert img == b"\xff"
    assert data == "a\nb"
    assert poss == [(0.0, 1.0, 2.0, 3.0, 4.0)]


def test_unpack_ragflow_table_item_legacy_tuple():
    img, data, poss = _unpack_ragflow_table_item((b"x", "t"))
    assert img == b"x"
    assert data == "t"
    assert poss == []
