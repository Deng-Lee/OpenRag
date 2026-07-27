import json
import logging

import pytest
from pypdf import PdfWriter

from openrag.parsers.adapters import paddleocr_pdf_adapter as adapter_module
from openrag.parsers.adapters.paddleocr_pdf_adapter import PaddleOCRPDFParserAdapter
from openrag.tracing.context import reset_trace_context, set_trace_context


@pytest.fixture(autouse=True)
def _clean_trace_context():
    reset_trace_context()
    yield
    reset_trace_context()


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def write_pdf(tmp_path, width=100, height=200, rotation=0):
    pdf = tmp_path / "sample.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    if rotation:
        page.rotate(rotation)
    with pdf.open("wb") as file_obj:
        writer.write(file_obj)
    return pdf


def _json_events(caplog):
    out = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("{"):
            continue
        out.append((record.name, json.loads(message)))
    return out


def test_success_response_creates_document_blocks(tmp_path, monkeypatch, caplog):
    pdf = write_pdf(tmp_path)
    post_calls = []
    get_calls = []

    def fake_post(url, files, data, timeout):
        post_calls.append((url, files, data, timeout))
        filename, file_obj = files["image"]
        assert filename == "sample.pdf"
        assert file_obj.read(5) == b"%PDF-"
        return FakeResponse({"task_id": "task-1"})

    def fake_get(url, timeout):
        get_calls.append((url, timeout))
        return FakeResponse(
            {
                "status": "done",
                "result": {
                    "pages": [
                        {
                            "width": 100,
                            "height": 200,
                            "doc_preprocessor_res": {"angle": 0},
                            "parsing_res_list": [
                                {
                                    "block_label": "title",
                                    "block_content": "Report Title",
                                    "block_bbox": [1, 2, 3, 4],
                                },
                                {
                                    "label": "doc_title",
                                    "content": "Doc Title",
                                    "bbox": [5, 6, 7, 8],
                                },
                                {
                                    "label": "table",
                                    "content": "A | B",
                                    "bbox": [9, 10, 11, 12],
                                },
                                {
                                    "label": "text",
                                    "content": "Bad bbox",
                                    "bbox": ["bad"],
                                },
                                {
                                    "label": "footer",
                                    "content": "   ",
                                    "bbox": [0, 0, 1, 1],
                                },
                            ]
                        }
                    ]
                },
            }
        )

    monkeypatch.setenv("PADDLEOCR_SERVER_URL", "http://ocr.test/")
    monkeypatch.setenv("PADDLEOCR_REQUEST_TIMEOUT", "9")
    monkeypatch.setenv("PADDLEOCR_POLL_TIMEOUT", "20")
    monkeypatch.setenv("PADDLEOCR_POLL_INTERVAL", "0")
    monkeypatch.setattr(adapter_module.requests, "post", fake_post)
    monkeypatch.setattr(adapter_module.requests, "get", fake_get)
    set_trace_context(task_id="trace-task", file_id=123)

    parser = PaddleOCRPDFParserAdapter()
    with caplog.at_level(logging.INFO, logger="paddleocr"):
        blocks = parser.parse(str(pdf))

    assert post_calls[0][0] == "http://ocr.test/paddleocr/async/ocr"
    assert post_calls[0][2] == {"doc_orientation": "True"}
    assert post_calls[0][3] == 9.0
    assert get_calls == [("http://ocr.test/paddleocr/async/task/task-1", 9.0)]
    assert [block.text for block in blocks] == [
        "Report Title",
        "Doc Title",
        "A | B",
        "Bad bbox",
    ]
    assert [block.page for block in blocks] == [1, 1, 1, 1]
    assert blocks[0].block_type == "text"
    assert blocks[0].level == 0
    assert blocks[0].metadata["ocr_label"] == "title"
    assert blocks[1].block_type == "text"
    assert blocks[1].metadata["ocr_label"] == "doc_title"
    assert blocks[2].block_type == "table"
    assert blocks[2].level == 0
    assert blocks[3].bbox is None
    assert blocks[0].bbox == (1.0, 2.0, 3.0, 4.0)
    assert [block.block_id for block in blocks] == [
        "paddleocr:page:1:block:0",
        "paddleocr:page:1:block:1",
        "paddleocr:page:1:block:2",
        "paddleocr:page:1:block:3",
    ]
    assert [(block.char_start, block.char_end) for block in blocks] == [
        (0, 12),
        (14, 23),
        (25, 30),
        (32, 40),
    ]
    assert parser.last_parse_stats["invalid_bbox_count"] == 1
    assert parser.last_parse_stats["scaled_bbox_count"] == 3
    assert parser.last_parse_stats["unmapped_bbox_count"] == 0
    assert all("line_positions" not in (block.metadata or {}) for block in blocks)

    profile = parser.last_parse_profile
    assert profile["task_id"] == "trace-task"
    assert profile["file_id"] == 123
    assert profile["file_path"] == str(pdf)
    assert isinstance(profile["total_ms"], int)
    assert profile["page_count"] == 1
    assert profile["block_count"] == 4
    assert profile["n_tables"] == 1
    assert profile["poll_count"] == 1
    assert profile["status"] == "ok"
    assert profile["invalid_bbox_count"] == 1
    assert profile["scaled_bbox_count"] == 3
    assert profile["unmapped_bbox_count"] == 0
    assert [stage["stage"] for stage in profile["stages"]] == [
        "paddleocr.submit",
        "paddleocr.poll",
        "paddleocr.fetch_result",
        "paddleocr.convert_blocks",
    ]
    assert all("duration_ms" in stage for stage in profile["stages"])
    assert all(stage["status"] == "ok" for stage in profile["stages"])

    events = _json_events(caplog)
    event_names = [event["evt"] for _logger_name, event in events]
    assert event_names == [
        "paddleocr.submit",
        "paddleocr.poll",
        "paddleocr.fetch_result",
        "paddleocr.convert_blocks",
        "paddleocr.parse_profile",
    ]
    assert {logger_name for logger_name, _event in events} == {
        "paddleocr.submit",
        "paddleocr.poll",
        "paddleocr.fetch_result",
        "paddleocr.convert_blocks",
        "paddleocr.parse_profile",
    }
    assert "Report Title" not in caplog.text
    profile_event = events[-1][1]
    assert profile_event["evt"] == "paddleocr.parse_profile"
    assert profile_event["page_count"] == 1
    assert profile_event["block_count"] == 4
    assert "file_path" not in profile_event
    assert "ocr_task_id" not in profile_event


def test_read_pdf_page_sizes_uses_display_dimensions(tmp_path):
    pdf = write_pdf(tmp_path, width=960, height=540)

    assert adapter_module._read_pdf_page_sizes(str(pdf)) == [(960.0, 540.0)]


def test_read_pdf_page_sizes_swaps_rotated_page_dimensions(tmp_path):
    pdf = write_pdf(tmp_path, width=960, height=540, rotation=90)

    assert adapter_module._read_pdf_page_sizes(str(pdf)) == [(540.0, 960.0)]


def test_read_pdf_page_sizes_returns_empty_when_page_tree_cannot_be_read(
    monkeypatch,
):
    class BrokenReader:
        @property
        def pages(self):
            raise RuntimeError("broken page tree")

    monkeypatch.setattr("pypdf.PdfReader", lambda _path: BrokenReader())

    assert adapter_module._read_pdf_page_sizes("broken.pdf") == []


def test_result_to_rows_scales_ocr_bbox_to_pdf_coordinates():
    result = {
        "pages": [
            {
                "width": 1920,
                "height": 1080,
                "doc_preprocessor_res": {"angle": 0},
                "parsing_res_list": [
                    {
                        "label": "text",
                        "content": "Scaled text",
                        "bbox": [543, 335, 1446, 609],
                    }
                ],
            }
        ]
    }

    rows, stats = adapter_module._result_to_rows(result, [(960.0, 540.0)])

    assert rows[0]["bbox"] == (271.5, 167.5, 723.0, 304.5)
    assert stats == {
        "invalid_bbox_count": 0,
        "scaled_bbox_count": 1,
        "unmapped_bbox_count": 0,
    }


def test_result_to_rows_rejects_non_monotonic_bbox():
    result = {
        "pages": [
            {
                "width": 100,
                "height": 200,
                "doc_preprocessor_res": {"angle": 0},
                "parsing_res_list": [
                    {"label": "text", "content": "Bad order", "bbox": [10, 20, 5, 30]}
                ],
            }
        ]
    }

    rows, stats = adapter_module._result_to_rows(result, [(100.0, 200.0)])

    assert rows[0]["bbox"] is None
    assert stats["invalid_bbox_count"] == 1


def test_result_to_rows_clamps_bbox_to_pdf_page():
    result = {
        "pages": [
            {
                "width": 100,
                "height": 200,
                "doc_preprocessor_res": {"angle": 0},
                "parsing_res_list": [
                    {"label": "text", "content": "Clamped", "bbox": [-10, -20, 110, 220]}
                ],
            }
        ]
    }

    rows, stats = adapter_module._result_to_rows(result, [(100.0, 200.0)])

    assert rows[0]["bbox"] == (0.0, 0.0, 100.0, 200.0)
    assert stats["scaled_bbox_count"] == 1


def test_result_to_rows_drops_bbox_when_clamp_leaves_no_area():
    result = {
        "pages": [
            {
                "width": 100,
                "height": 200,
                "doc_preprocessor_res": {"angle": 0},
                "parsing_res_list": [
                    {"label": "text", "content": "Outside", "bbox": [110, 10, 120, 20]}
                ],
            }
        ]
    }

    rows, stats = adapter_module._result_to_rows(result, [(100.0, 200.0)])

    assert rows[0]["bbox"] is None
    assert stats["unmapped_bbox_count"] == 1


def test_result_to_rows_scales_when_rotated_page_orientation_matches():
    result = {
        "pages": [
            {
                "width": 1080,
                "height": 1920,
                "doc_preprocessor_res": {"angle": 0},
                "parsing_res_list": [
                    {"label": "text", "content": "Portrait", "bbox": [2, 4, 6, 8]}
                ],
            }
        ]
    }

    rows, stats = adapter_module._result_to_rows(result, [(540.0, 960.0)])

    assert rows[0]["bbox"] == (1.0, 2.0, 3.0, 4.0)
    assert stats["scaled_bbox_count"] == 1


def test_result_to_rows_drops_bbox_when_page_orientation_does_not_match():
    result = {
        "pages": [
            {
                "width": 1920,
                "height": 1080,
                "doc_preprocessor_res": {"angle": 0},
                "parsing_res_list": [
                    {"label": "text", "content": "Landscape", "bbox": [2, 4, 6, 8]}
                ],
            }
        ]
    }

    rows, stats = adapter_module._result_to_rows(result, [(540.0, 960.0)])

    assert rows[0]["text"] == "Landscape"
    assert rows[0]["bbox"] is None
    assert stats["scaled_bbox_count"] == 0
    assert stats["unmapped_bbox_count"] == 1


def test_result_to_rows_degrades_only_page_without_pdf_geometry():
    result = {
        "pages": [
            {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {"label": "text", "content": "Mapped", "bbox": [1, 2, 3, 4]}
                ],
            },
            {
                "width": 100,
                "height": 200,
                "parsing_res_list": [
                    {"label": "text", "content": "Unmapped", "bbox": [5, 6, 7, 8]}
                ],
            },
        ]
    }

    rows, stats = adapter_module._result_to_rows(
        result, [(100.0, 200.0), None]
    )

    assert rows[0]["bbox"] == (1.0, 2.0, 3.0, 4.0)
    assert rows[1]["text"] == "Unmapped"
    assert rows[1]["bbox"] is None
    assert stats["scaled_bbox_count"] == 1
    assert stats["unmapped_bbox_count"] == 1


@pytest.mark.parametrize(
    "page",
    [
        {"width": None, "height": 1080, "doc_preprocessor_res": {"angle": 0}},
        {"width": 1920, "height": 1080, "doc_preprocessor_res": {"angle": 90}},
    ],
)
def test_result_to_rows_keeps_text_when_bbox_cannot_be_mapped(page):
    page["parsing_res_list"] = [
        {"label": "text", "content": "Keep me", "bbox": [1, 2, 3, 4]}
    ]

    rows, stats = adapter_module._result_to_rows(
        {"pages": [page]}, [(960.0, 540.0)]
    )

    assert rows[0]["text"] == "Keep me"
    assert rows[0]["bbox"] is None
    assert stats["invalid_bbox_count"] == 0
    assert stats["scaled_bbox_count"] == 0
    assert stats["unmapped_bbox_count"] == 1


def test_missing_server_url_raises_clear_error(tmp_path, monkeypatch):
    pdf = write_pdf(tmp_path)
    monkeypatch.delenv("PADDLEOCR_SERVER_URL", raising=False)

    with pytest.raises(RuntimeError, match="PADDLEOCR_SERVER_URL"):
        PaddleOCRPDFParserAdapter().parse(str(pdf))


def test_parse_rejects_non_pdf_before_submit(tmp_path, monkeypatch):
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"not a real pdf")
    monkeypatch.setenv("PADDLEOCR_SERVER_URL", "http://ocr.test")
    post_called = False

    def fake_post(*args, **kwargs):
        nonlocal post_called
        post_called = True
        return FakeResponse({"task_id": "task-1"})

    monkeypatch.setattr(adapter_module.requests, "post", fake_post)

    with pytest.raises(RuntimeError, match="only supports PDF"):
        PaddleOCRPDFParserAdapter().parse(str(fake_pdf))

    assert post_called is False


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("failed", "failed"),
        ("not_found", "not_found"),
    ],
)
def test_remote_terminal_errors_raise_clear_error(
    tmp_path, monkeypatch, status, message, caplog
):
    pdf = write_pdf(tmp_path)
    monkeypatch.setenv("PADDLEOCR_SERVER_URL", "http://ocr.test")
    monkeypatch.setattr(
        adapter_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse({"task_id": "task-1"}),
    )
    monkeypatch.setattr(
        adapter_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"status": status, "message": message}),
    )
    set_trace_context(task_id="trace-task", file_id=123)

    parser = PaddleOCRPDFParserAdapter()
    expected_summary = f"PaddleOCR task {status}"
    with pytest.raises(RuntimeError, match=expected_summary):
        with caplog.at_level(logging.INFO, logger="paddleocr"):
            parser.parse(str(pdf))

    profile = parser.last_parse_profile
    assert profile["task_id"] == "trace-task"
    assert profile["file_id"] == 123
    assert profile["file_path"] == str(pdf)
    assert profile["status"] == "error"
    assert profile["poll_count"] == 1
    assert [stage["stage"] for stage in profile["stages"]] == [
        "paddleocr.submit",
        "paddleocr.poll",
    ]
    assert profile["stages"][-1]["status"] == "error"
    assert profile["stages"][-1]["error_type"] == "RuntimeError"
    assert profile["stages"][-1]["error_summary"] == expected_summary
    assert "Report Title" not in caplog.text


def test_remote_error_message_is_not_written_to_structured_logs(
    tmp_path, monkeypatch, caplog
):
    pdf = write_pdf(tmp_path)
    monkeypatch.setenv("PADDLEOCR_SERVER_URL", "http://ocr.test")
    monkeypatch.setattr(
        adapter_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse({"task_id": "task-1"}),
    )
    monkeypatch.setattr(
        adapter_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"status": "failed", "message": "SECRET OCR TEXT"}
        ),
    )

    parser = PaddleOCRPDFParserAdapter()
    with pytest.raises(RuntimeError) as exc_info:
        with caplog.at_level(logging.INFO, logger="paddleocr"):
            parser.parse(str(pdf))

    assert str(exc_info.value) == "PaddleOCR task failed"
    assert "SECRET OCR TEXT" not in str(exc_info.value)
    assert "SECRET OCR TEXT" not in caplog.text
    assert "SECRET OCR TEXT" not in str(parser.last_parse_profile)
    assert (
        parser.last_parse_profile["stages"][-1]["error_summary"]
        == "PaddleOCR task failed"
    )


def test_timeout_raises_clear_error(tmp_path, monkeypatch):
    pdf = write_pdf(tmp_path)
    monkeypatch.setenv("PADDLEOCR_SERVER_URL", "http://ocr.test")
    monkeypatch.setenv("PADDLEOCR_POLL_TIMEOUT", "0")
    monkeypatch.setenv("PADDLEOCR_POLL_INTERVAL", "0")
    monkeypatch.setattr(
        adapter_module.requests,
        "post",
        lambda *args, **kwargs: FakeResponse({"task_id": "task-1"}),
    )
    monkeypatch.setattr(
        adapter_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"status": "running"}),
    )
    monkeypatch.setattr(adapter_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="timed out"):
        PaddleOCRPDFParserAdapter().parse(str(pdf))
