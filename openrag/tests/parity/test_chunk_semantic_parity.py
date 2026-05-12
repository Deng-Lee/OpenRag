"""Parity tests: ragflow_core semantic chunk helpers."""

from openrag.chunking.ragflow_core.semantic import chunk_semantic_ragflow, ragflow_semantic_chunk
from openrag.parsers.base import DocumentBlock


def test_ragflow_semantic_chunk_respects_children_delimiter():
    parts = ragflow_semantic_chunk(
        ["A。B。C。"],
        chunk_token_num=128,
        delimiter="\n!?;。；！？",
        children_delimiter="。",
    )
    assert len(parts) >= 2


def test_children_delimiter_sets_mom_with_weight_on_subchunks(monkeypatch):
    """When OPENRAG_CHILDREN_DELIMITER is set, child chunks get ``mom_with_weight``."""
    monkeypatch.setenv("OPENRAG_CHILDREN_DELIMITER", "。")
    monkeypatch.setenv("OPENRAG_CHUNK_OVERLAPPED_PERCENT", "0")

    block = DocumentBlock(
        text="第一段内容。第二段内容。",
        page=1,
        offset=0,
        char_start=0,
        char_end=32,
        block_type="text",
    )

    def _no_fixed(tb, cs, co):
        raise AssertionError("unexpected fixed_size fallback")

    chunks = chunk_semantic_ragflow(
        [block],
        256,
        0,
        None,
        fixed_size_fallback=_no_fixed,
    )
    moms = [
        (c.metadata or {}).get("mom_with_weight")
        for c in chunks
        if c.metadata and (c.metadata or {}).get("mom_with_weight")
    ]
    assert moms, "expected at least one chunk with mom_with_weight when children delimiter active"
