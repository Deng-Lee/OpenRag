"""Parity tests: ragflow_core chunk metadata fields."""

from openrag.chunking.ragflow_core.metadata import build_chunk_metadata


def test_build_chunk_metadata_contains_ragflow_fields():
    md = build_chunk_metadata("hello", ck_type="text", positions=[(0, 1, 2, 3, 4)])
    assert "content_ltks" in md
    assert "content_sm_ltks" in md
    assert "position_int" in md
    assert md["doc_type_kwd"] == "text"
    assert md["page_num_int"] == [1]
    assert md["top_int"] == [3]
