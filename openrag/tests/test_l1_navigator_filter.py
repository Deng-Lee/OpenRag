"""Tests for L1 LLM navigator chunk index filtering (no live API calls)."""

from openrag.retrieval.l1_llm_navigator import filter_chunk_hits_by_indices


def test_filter_keeps_matching_indices():
    hits = [
        {"file_id": 1, "chunk_index": 0, "text": "a"},
        {"file_id": 1, "chunk_index": 2, "text": "b"},
        {"file_id": 2, "chunk_index": 0, "text": "c"},
    ]
    r = {1: {2}, 2: {0}}
    out = filter_chunk_hits_by_indices(hits, r)
    assert len(out) == 2
    assert {h["chunk_index"] for h in out if h["file_id"] == 1} == {2}
    assert {h["chunk_index"] for h in out if h["file_id"] == 2} == {0}


def test_filter_unlisted_file_unchanged():
    hits = [{"file_id": 99, "chunk_index": 5}]
    r = {1: {0}}
    out = filter_chunk_hits_by_indices(hits, r)
    assert out == hits


def test_filter_empty_restrictions_returns_original():
    hits = [{"file_id": 1, "chunk_index": 0}]
    assert filter_chunk_hits_by_indices(hits, {}) is hits


def test_filter_all_removed_falls_back_to_original():
    hits = [{"file_id": 1, "chunk_index": 0}]
    r = {1: {99}}
    out = filter_chunk_hits_by_indices(hits, r)
    assert out == hits
