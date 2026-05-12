"""Parity tests: ragflow_core compat mapping to DocumentBlock."""

import pytest

from openrag.ragflow_core.compat import to_document_blocks


def test_to_document_blocks_preserves_bbox_and_page():
    raw = [
        {
            "text": "hello",
            "page": 2,
            "bbox": (1.0, 2.0, 3.0, 4.0),
            "block_type": "text",
        }
    ]
    blocks = to_document_blocks(raw, source="pdf")
    assert blocks[0].text == "hello"
    assert blocks[0].page == 2
    assert blocks[0].bbox == (1.0, 2.0, 3.0, 4.0)
    assert blocks[0].metadata is not None
    assert blocks[0].metadata.get("compat_source") == "pdf"


def test_to_document_blocks_rejects_malformed_bbox():
    raw = [{"text": "x", "bbox": (1, 2, 3)}]
    blocks = to_document_blocks(raw, source="docx")
    assert blocks[0].bbox is None


def test_jsonl_parse_does_not_call_ragflow_json_binary_parser(tmp_path):
    """``.jsonl`` path uses line parser; RAGFlow JSON blob parser must not run."""
    from openrag.parsers.adapters.json_adapter import JsonParserAdapter

    p = tmp_path / "sample.jsonl"
    p.write_text('{"x": 1}\n{"y": 2}\n', encoding="utf-8")

    adapter = object.__new__(JsonParserAdapter)
    adapter.ragflow_parser = lambda _bin: (_ for _ in ()).throw(
        AssertionError("RAGFlowJsonParser should not be used for .jsonl in this test")
    )
    blocks = JsonParserAdapter.parse(adapter, str(p))
    assert len(blocks) == 2
    assert "1" in blocks[0].text or "x" in blocks[0].text


def test_convert_doc_to_docx_raises_clear_error_when_no_backend(tmp_path, monkeypatch):
    """Legacy .doc conversion: explicit error when Word/LibreOffice both unavailable."""
    from openrag.parsers.adapters import docx_adapter as da

    legacy = tmp_path / "legacy.doc"
    legacy.write_bytes(b"0")
    monkeypatch.setattr(da, "_convert_via_word_subprocess", lambda *a: None)
    monkeypatch.setattr(da, "_convert_via_libreoffice", lambda *a: None)
    with pytest.raises(RuntimeError, match="无法解析|docx"):
        da._convert_doc_to_docx(str(legacy))
