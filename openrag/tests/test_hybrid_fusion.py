from copy import deepcopy

import pytest

from openrag.retrieval.hybrid_fusion import weighted_rrf


def test_weighted_rrf_unions_dense_and_sparse_candidates():
    dense = [
        {"chunk_id": "a", "file_id": 1, "text": "a", "score": 0.9},
        {"chunk_id": "b", "file_id": 2, "text": "b", "score": 0.8},
        {"chunk_id": "c", "file_id": 3, "text": "c", "score": 0.7},
    ]
    sparse = [
        {"chunk_id": "d", "file_id": 4, "text": "d", "sparse_score": 100.0},
        {"chunk_id": "a", "file_id": 1, "text": "a", "sparse_score": 3.0},
    ]

    fused = weighted_rrf(dense, sparse, dense_weight=0.5)
    by_id = {hit["chunk_id"]: hit for hit in fused}

    assert set(by_id) == {"a", "b", "c", "d"}
    assert by_id["d"]["dense_rank"] is None
    assert by_id["d"]["sparse_rank"] == 1
    assert by_id["d"]["recall_sources"] == ["sparse"]
    assert by_id["a"]["dense_rank"] == 1
    assert by_id["a"]["sparse_rank"] == 2
    assert by_id["a"]["recall_sources"] == ["dense", "sparse"]
    assert by_id["a"]["dense_score"] == 0.9
    assert by_id["a"]["sparse_score"] == 3.0


def test_weighted_rrf_depends_on_rank_not_raw_score_scale():
    dense = [{"chunk_id": "a", "score": 0.9}, {"chunk_id": "b", "score": 0.8}]
    low = [{"chunk_id": "c", "sparse_score": 2.0}, {"chunk_id": "a", "sparse_score": 1.0}]
    high = [{"chunk_id": "c", "sparse_score": 2000.0}, {"chunk_id": "a", "sparse_score": 1000.0}]

    assert [hit["chunk_id"] for hit in weighted_rrf(dense, low, dense_weight=0.5)] == [
        hit["chunk_id"] for hit in weighted_rrf(dense, high, dense_weight=0.5)
    ]


@pytest.mark.parametrize(
    ("weight", "expected"),
    [(1.0, ["dense"]), (0.0, ["sparse"])],
)
def test_weighted_rrf_zero_weight_channel_does_not_add_candidates(weight, expected):
    fused = weighted_rrf(
        [{"chunk_id": "dense", "score": 0.9}],
        [{"chunk_id": "sparse", "sparse_score": 10.0}],
        dense_weight=weight,
    )
    assert [hit["chunk_id"] for hit in fused] == expected


@pytest.mark.parametrize("weight", [-0.1, 1.1])
def test_weighted_rrf_rejects_invalid_weight(weight):
    with pytest.raises(ValueError, match="dense_weight"):
        weighted_rrf([], [], dense_weight=weight)


def test_weighted_rrf_is_stable_deduplicated_and_does_not_mutate_inputs():
    dense = [
        {"chunk_id": "b", "score": 0.8, "metadata": {"x": 1}},
        {"chunk_id": "a", "score": 0.8},
        {"chunk_id": "b", "score": 0.7},
    ]
    sparse = [
        {"chunk_id": "a", "sparse_score": 2.0},
        {"chunk_id": "b", "sparse_score": 1.0},
    ]
    original_dense = deepcopy(dense)
    original_sparse = deepcopy(sparse)

    first = weighted_rrf(dense, sparse, dense_weight=0.5, rrf_k=60, limit=2)
    second = weighted_rrf(dense, sparse, dense_weight=0.5, rrf_k=60, limit=2)

    assert [hit["chunk_id"] for hit in first] == ["a", "b"]
    assert first == second
    assert len(first) == 2
    assert dense == original_dense
    assert sparse == original_sparse
