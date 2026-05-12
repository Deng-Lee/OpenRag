"""RAGFlow PDF 位置标签：制表符或空格分隔均需可解析（入库 bbox 依赖此）。"""

from openrag.parsers.adapters.pdf_adapter import (
    _page_bbox_from_ragflow_table_positions,
    _plain_page_bbox_from_tagged_paragraph,
    _unpack_ragflow_table_item,
)
from openrag.parsers.ragflow.parser.pdf_parser import RAGFlowPdfParser


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


def test_ragflow_remove_tag_and_extract_positions_space_form():
    s = "x@@2 1.0 2.0 3.0 4.0##"
    assert "@@" not in RAGFlowPdfParser.remove_tag(s)
    poss = RAGFlowPdfParser.extract_positions(s)
    assert len(poss) == 1
    assert poss[0][0] == [1]  # 0-based page index
    assert poss[0][1:] == (1.0, 2.0, 3.0, 4.0)


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
