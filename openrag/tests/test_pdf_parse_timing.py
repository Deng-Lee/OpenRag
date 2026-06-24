"""Tests for RAGFlow PDF parser per-stage timing instrumentation."""

import json
import logging

import pytest

from openrag.parsers.ragflow.parser import pdf_parser as pdf_parser_module
from openrag.tracing.context import reset_trace_context, set_trace_context

RAGFlowPdfParser = pdf_parser_module.RAGFlowPdfParser

STAGE_NAMES = [
    "pdf.images_ocr",
    "pdf.layout_recognition",
    "pdf.table_transformer",
    "pdf.text_merge",
    "pdf.concat_downward",
    "pdf.filter_forpages",
    "pdf.extract_table_figure",
    "pdf.filterout_scraps",
]


@pytest.fixture(autouse=True)
def _clean_trace_ctx():
    reset_trace_context()
    yield
    reset_trace_context()


def _bare_parser():
    """Create a parser instance without running the heavy __init__."""
    p = RAGFlowPdfParser.__new__(RAGFlowPdfParser)
    p._parse_file_path = "/tmp/doc_7_report.pdf"
    p._stage_profile = []
    p.boxes = []
    p.page_images = []
    return p


def _json_events(caplog, evt):
    """Return [(logger_name, parsed_json), ...] for records whose JSON evt matches."""
    out = []
    for r in caplog.records:
        msg = r.getMessage()
        if not msg.startswith("{"):
            continue
        try:
            d = json.loads(msg)
        except ValueError:
            continue
        if d.get("evt") == evt:
            out.append((r.name, d))
    return out


def test_stage_emits_json_with_context(caplog):
    set_trace_context(task_id="t1", file_id=42)
    p = _bare_parser()
    p.boxes = [1, 2, 3]
    with caplog.at_level(logging.INFO, logger="pdf"):
        with p._stage("pdf.layout_recognition"):
            p.boxes = [1, 2]  # simulate the stage mutating boxes
    events = _json_events(caplog, "pdf_stage")
    assert len(events) == 1
    name, d = events[0]
    assert name == "pdf.layout_recognition"
    assert d["stage"] == "pdf.layout_recognition"
    assert d["task_id"] == "t1"
    assert d["file_id"] == 42
    assert d["file_path"] == "/tmp/doc_7_report.pdf"
    assert isinstance(d["duration_ms"], int)
    assert d["boxes_before"] == 3
    assert d["boxes_after"] == 2
    assert d["status"] == "ok"
    assert p._stage_profile == [
        {"stage": "pdf.layout_recognition", "duration_ms": d["duration_ms"]}
    ]


def test_stage_logs_error_and_reraises(caplog):
    set_trace_context(task_id="t1", file_id=42)
    p = _bare_parser()
    with caplog.at_level(logging.INFO, logger="pdf"):
        with pytest.raises(ValueError):
            with p._stage("pdf.text_merge"):
                raise ValueError("boom")
    events = _json_events(caplog, "pdf_stage")
    assert len(events) == 1
    _, d = events[0]
    assert d["stage"] == "pdf.text_merge"
    assert d["status"] == "error"
    assert d["error"] == "ValueError"


def test_stage_without_trace_context(caplog):
    p = _bare_parser()
    with caplog.at_level(logging.INFO, logger="pdf"):
        with p._stage("pdf.images_ocr"):
            pass
    events = _json_events(caplog, "pdf_stage")
    assert len(events) == 1
    _, d = events[0]
    assert d["task_id"] is None
    assert d["file_id"] is None
    assert d["status"] == "ok"
