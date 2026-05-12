"""Regression tests for RAGFlow PDF parser error paths."""

from openrag.parsers.ragflow.parser import pdf_parser as pdf_parser_module


def test_images_handles_pdfplumber_failure_without_missing_attrs(monkeypatch):
    """When pdfplumber fails early, parser should keep safe defaults."""
    parser = pdf_parser_module.RAGFlowPdfParser.__new__(pdf_parser_module.RAGFlowPdfParser)
    parser.parallel_limiter = None
    parser._has_color = lambda c: True
    parser._is_garbled_text = lambda text, threshold=0.3: False
    parser._is_garbled_by_font_encoding = lambda chars, min_chars=20: False

    def _raise_open(*_args, **_kwargs):
        raise RuntimeError("boom")

    def _raise_pdf2(*_args, **_kwargs):
        raise RuntimeError("boom2")

    monkeypatch.setattr(pdf_parser_module.pdfplumber, "open", _raise_open)
    monkeypatch.setattr(pdf_parser_module, "pdf2_read", _raise_pdf2)

    parser.__images__("dummy.pdf")

    assert parser.page_chars == []
    assert parser.page_images == []
