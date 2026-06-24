"""Persist the PDF stage-timing profile through adapter -> parse.document span."""


def test_adapter_exposes_last_parse_profile():
    from openrag.parsers.adapters.pdf_adapter import PDFParserAdapter

    adapter = PDFParserAdapter.__new__(PDFParserAdapter)  # skip heavy __init__
    adapter.last_parse_profile = None

    class _FakeRagflowParser:
        _last_parse_profile = {
            "stages": [{"stage": "pdf.images_ocr", "duration_ms": 5}],
            "total_ms": 5,
            "page_count": 1,
            "n_tables": 0,
            "status": "ok",
        }

        def __call__(self, fnm):
            return ("hello world", [])

    adapter.ragflow_parser = _FakeRagflowParser()
    adapter._parse_ragflow_with_tables("x.pdf")
    assert adapter.last_parse_profile is not None
    assert adapter.last_parse_profile["stages"][0]["stage"] == "pdf.images_ocr"
    assert adapter.last_parse_profile["total_ms"] == 5


def test_parse_span_profile_extracts_clean_subset():
    from openrag.processors.document_processor import _parse_span_profile

    class _P:
        last_parse_profile = {
            "evt": "pdf_parse_profile",
            "task_id": "t",
            "file_id": 1,
            "file_path": "/x.pdf",
            "page_count": 3,
            "total_ms": 100,
            "n_tables": 2,
            "status": "ok",
            "stages": [{"stage": "pdf.images_ocr", "duration_ms": 50}],
        }

    out = _parse_span_profile(_P())
    assert out == {
        "total_ms": 100,
        "page_count": 3,
        "n_tables": 2,
        "status": "ok",
        "stages": [{"stage": "pdf.images_ocr", "duration_ms": 50}],
    }


def test_parse_span_profile_none_for_non_pdf_parser():
    from openrag.processors.document_processor import _parse_span_profile

    class _P:  # e.g. a txt/docx adapter that has no last_parse_profile
        pass

    assert _parse_span_profile(_P()) is None


def test_parse_span_profile_none_when_no_stages():
    from openrag.processors.document_processor import _parse_span_profile

    class _P:
        last_parse_profile = {"status": "ok", "stages": []}

    assert _parse_span_profile(_P()) is None
