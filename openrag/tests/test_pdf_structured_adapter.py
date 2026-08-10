from openrag.parsers.adapters.pdf_adapter import PDFParserAdapter


class _StructuredParser:
    _last_parse_profile = {
        "stages": [{"stage": "pdf.layout_recognition", "duration_ms": 2}],
        "total_ms": 2,
        "status": "ok",
    }
    outlines = [("第一章 总则", 0, 1)]

    def parse_into_bboxes(self, _path):
        return [
            {
                "text": "第一章 总则",
                "page_number": 1,
                "x0": 10,
                "x1": 100,
                "top": 20,
                "bottom": 40,
                "layout_type": "title",
                "layoutno": "title-0",
                "layout_score": 0.93,
                "positions": [[1, 10, 100, 20, 40]],
            },
            {
                "text": "正文 A",
                "page_number": 1,
                "x0": 10,
                "x1": 100,
                "top": 50,
                "bottom": 70,
                "layout_type": "text",
                "layoutno": "text-0",
                "positions": [[1, 10, 100, 50, 70]],
            },
            {
                "text": "<table><tr><td>A</td></tr></table>",
                "page_number": 1,
                "x0": 10,
                "x1": 100,
                "top": 80,
                "bottom": 120,
                "layout_type": "table",
                "image": b"table-image",
                "positions": [[1, 10, 100, 80, 120]],
            },
            {
                "text": "正文 B",
                "page_number": 1,
                "x0": 10,
                "x1": 100,
                "top": 130,
                "bottom": 150,
                "layout_type": "text",
                "layoutno": "text-1",
                "positions": [[1, 10, 100, 130, 150]],
            },
            {
                "text": "架构图",
                "page_number": 2,
                "x0": 20,
                "x1": 120,
                "top": 20,
                "bottom": 80,
                "layout_type": "figure",
                "image": b"figure-image",
                "positions": [[2, 20, 120, 20, 80]],
            },
        ]


def test_structured_adapter_preserves_order_types_and_heading_evidence():
    adapter = PDFParserAdapter.__new__(PDFParserAdapter)
    adapter.ragflow_parser = _StructuredParser()
    adapter.last_parse_profile = None

    blocks = adapter._parse_ragflow_structured("dummy.pdf")

    assert [block.text for block in blocks] == [
        "第一章 总则",
        "正文 A",
        "<table><tr><td>A</td></tr></table>",
        "正文 B",
        "架构图",
    ]
    assert [block.block_type for block in blocks] == [
        "heading",
        "text",
        "table",
        "text",
        "image",
    ]
    assert blocks[0].level == 1
    assert blocks[0].metadata["hard_boundary"] is True
    assert blocks[0].metadata["heading_sources"] == ["outline"]
    assert blocks[0].metadata["deepdoc_layout_score"] == 0.93
    assert blocks[2].bbox == (10.0, 80.0, 100.0, 120.0)
    assert blocks[2].image == b"table-image"
    assert blocks[4].page == 2
    assert blocks[4].block_type == "image"
    assert adapter.last_parse_profile["status"] == "ok"


def test_parse_uses_structured_path_when_available():
    adapter = PDFParserAdapter.__new__(PDFParserAdapter)
    adapter.ragflow_parser = _StructuredParser()
    adapter.last_parse_profile = None

    blocks = adapter.parse("dummy.pdf")

    assert blocks[0].block_type == "heading"
    assert blocks[2].block_type == "table"
