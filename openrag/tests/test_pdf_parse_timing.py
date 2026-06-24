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


def test_emit_parse_profile(caplog):
    set_trace_context(task_id="t9", file_id=7)
    p = _bare_parser()
    p.page_images = [object(), object(), object()]
    p._stage_profile = [
        {"stage": "pdf.images_ocr", "duration_ms": 100},
        {"stage": "pdf.layout_recognition", "duration_ms": 50},
    ]
    with caplog.at_level(logging.INFO, logger="pdf"):
        p._emit_parse_profile(total_ms=200, n_tables=3, status="ok")
    events = _json_events(caplog, "pdf_parse_profile")
    assert len(events) == 1
    name, d = events[0]
    assert name == "pdf.parse_profile"
    assert d["task_id"] == "t9"
    assert d["file_id"] == 7
    assert d["file_path"] == "/tmp/doc_7_report.pdf"
    assert d["page_count"] == 3
    assert d["total_ms"] == 200
    assert d["n_tables"] == 3
    assert d["status"] == "ok"
    assert len(d["stages"]) == 2
    assert d["stages"][0]["stage"] == "pdf.images_ocr"


def _stub_all_stages(p, tbls=None):
    """Replace the 8 stage methods with no-op stubs (name-mangled dunders included)."""
    tbls = tbls if tbls is not None else []
    # __images__ has two trailing underscores -> NOT name-mangled -> attr is "__images__".
    # __filterout_scraps has no trailing underscore -> mangled -> "_RAGFlowPdfParser__filterout_scraps".
    setattr(p, "__images__", lambda *a, **k: None)
    p._layouts_rec = lambda *a, **k: None
    p._table_transformer_job = lambda *a, **k: None
    p._text_merge = lambda *a, **k: None
    p._concat_downward = lambda *a, **k: None
    p._filter_forpages = lambda *a, **k: None
    p._extract_table_figure = lambda *a, **k: tbls
    p._RAGFlowPdfParser__filterout_scraps = lambda *a, **k: "BODY TEXT"


def test_call_emits_all_stage_lines_and_profile(caplog):
    set_trace_context(task_id="tc", file_id=11)
    p = _bare_parser()
    _stub_all_stages(p, tbls=[("img", "tbl")])
    with caplog.at_level(logging.INFO, logger="pdf"):
        text, tbls = RAGFlowPdfParser.__call__(p, "/tmp/doc_11_a.pdf")
    assert text == "BODY TEXT"
    assert len(tbls) == 1
    stage_events = _json_events(caplog, "pdf_stage")
    assert [d["stage"] for _, d in stage_events] == STAGE_NAMES  # order preserved
    for _, d in stage_events:
        assert d["task_id"] == "tc"
        assert d["file_id"] == 11
        assert d["file_path"] == "/tmp/doc_11_a.pdf"
        assert d["status"] == "ok"
    profile = _json_events(caplog, "pdf_parse_profile")
    assert len(profile) == 1
    _, pd = profile[0]
    assert len(pd["stages"]) == 8
    assert pd["n_tables"] == 1
    assert pd["status"] == "ok"
    assert isinstance(pd["total_ms"], int)


def test_call_emits_profile_on_failure(caplog):
    set_trace_context(task_id="tc", file_id=11)
    p = _bare_parser()
    _stub_all_stages(p)

    def _boom(*a, **k):
        raise RuntimeError("layout failed")

    p._layouts_rec = _boom
    with caplog.at_level(logging.INFO, logger="pdf"):
        with pytest.raises(RuntimeError):
            RAGFlowPdfParser.__call__(p, "/tmp/doc_11_a.pdf")
    by_stage = {d["stage"]: d for _, d in _json_events(caplog, "pdf_stage")}
    assert by_stage["pdf.images_ocr"]["status"] == "ok"
    assert by_stage["pdf.layout_recognition"]["status"] == "error"
    assert by_stage["pdf.layout_recognition"]["error"] == "RuntimeError"
    assert "pdf.text_merge" not in by_stage  # never reached after the failure
    profile = _json_events(caplog, "pdf_parse_profile")
    assert len(profile) == 1
    assert profile[0][1]["status"] == "error"


def test_call_stores_last_parse_profile():
    set_trace_context(task_id="tc", file_id=11)
    p = _bare_parser()
    _stub_all_stages(p, tbls=[("img", "tbl")])
    RAGFlowPdfParser.__call__(p, "/tmp/doc_11_a.pdf")
    prof = p._last_parse_profile
    assert isinstance(prof, dict)
    assert prof["status"] == "ok"
    assert isinstance(prof["total_ms"], int)
    assert prof["n_tables"] == 1
    assert len(prof["stages"]) == 8
    assert prof["file_path"] == "/tmp/doc_11_a.pdf"
