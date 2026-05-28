from dataclasses import dataclass

import pytest

from openrag.evaluation.metrics import (
    average_precision_at_k,
    hit_rate_at_k,
    map_at_k,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    rerank_delta,
    stage_recall_at_k,
)
from openrag.evaluation.schemas import EvalJudgmentItem, RetrievalResultItem


@dataclass
class ObjectResult:
    chunk_id: str
    score: float = 0.0


class PydanticLikeJudgment:
    def __init__(self, chunk_id, relevance_grade, source="gold_manual", weight=None):
        self.chunk_id = chunk_id
        self.relevance_grade = relevance_grade
        self.source = source
        self.weight = weight


def test_binary_metrics_handle_empty_results_and_no_positive_judgments():
    judgments = [
        {"chunk_id": "a", "relevance_grade": 1, "source": "gold_manual"},
        {"chunk_id": "b", "relevance_grade": 0, "source": "business_import"},
    ]

    assert precision_at_k([], judgments, 10) == 0.0
    assert recall_at_k([], judgments, 10) == 0.0
    assert hit_rate_at_k([], judgments, 10) == 0.0
    assert mrr_at_k([], judgments) == 0.0
    assert average_precision_at_k([], judgments) == 0.0

    results = [{"chunk_id": "a"}, {"chunk_id": "b"}]
    assert recall_at_k(results, judgments, 2) == 0.0
    assert hit_rate_at_k(results, judgments, 2) == 0.0


def test_precision_recall_hit_mrr_and_ap_deduplicate_retrieved_chunks_and_respect_k():
    results = [
        RetrievalResultItem(chunk_id="irrelevant"),
        {"chunk_id": "rel-1"},
        ObjectResult(chunk_id="rel-1"),
        {"chunk_id": "rel-2"},
    ]
    judgments = [
        EvalJudgmentItem(chunk_id="rel-1", relevance_grade=2),
        PydanticLikeJudgment("rel-2", 3),
        {"chunk_id": "irrelevant", "relevance_grade": 1, "source": "gold_manual"},
    ]

    assert precision_at_k(results, judgments, 2) == pytest.approx(0.5)
    assert precision_at_k(results, judgments, 4) == pytest.approx(0.5)
    assert recall_at_k(results, judgments, 2) == pytest.approx(0.5)
    assert recall_at_k(results, judgments, 4) == pytest.approx(1.0)
    assert hit_rate_at_k(results, judgments, 1) == 0.0
    assert hit_rate_at_k(results, judgments, 2) == 1.0
    assert mrr_at_k(results, judgments, k=4) == pytest.approx(0.5)
    assert average_precision_at_k(results, judgments, k=4) == pytest.approx((1 / 2 + 2 / 4) / 2)


def test_ndcg_uses_raw_zero_to_three_gain_and_ignores_duplicate_hits():
    results = [
        {"chunk_id": "b"},
        {"chunk_id": "a"},
        {"chunk_id": "b"},
        {"chunk_id": "c"},
    ]
    judgments = [
        {"chunk_id": "a", "relevance_grade": 3},
        {"chunk_id": "b", "relevance_grade": 2},
        {"chunk_id": "c", "relevance_grade": 0},
        {"chunk_id": "d", "relevance_grade": 1},
    ]

    second_rank_discount = 1.5849625007211563
    third_rank_discount = 2.0
    dcg = 2 / 1 + 3 / second_rank_discount
    ideal_dcg = 3 / 1 + 2 / second_rank_discount + 1 / third_rank_discount

    assert ndcg_at_k(results, judgments, 4) == pytest.approx(dcg / ideal_dcg)


def test_source_scope_filters_single_source_and_weighted_all_applies_abc_weights():
    results = [{"chunk_id": "gold"}, {"chunk_id": "biz"}, {"chunk_id": "llm"}]
    judgments = [
        {"chunk_id": "gold", "relevance_grade": 2, "source": "gold_manual"},
        {"chunk_id": "biz", "relevance_grade": 2, "source": "business_import"},
        {"chunk_id": "llm", "relevance_grade": 2, "source": "llm_assisted"},
        {"chunk_id": "noise", "relevance_grade": 1, "source": "gold_manual"},
    ]

    assert precision_at_k(results, judgments, 3, source_scope="gold_manual") == pytest.approx(1 / 3)
    assert recall_at_k(results, judgments, 3, source_scope="business_import") == 1.0
    assert hit_rate_at_k(results, judgments, 2, source_scope="llm_assisted") == 0.0
    assert precision_at_k(results, judgments, 3, source_scope="weighted_all") == pytest.approx(
        (1.0 + 0.7 + 0.4) / 3
    )
    assert recall_at_k(results[:2], judgments, 2, source_scope="weighted_all") == pytest.approx(
        (1.0 + 0.7) / (1.0 + 0.7 + 0.4)
    )


def test_explicit_judgment_weight_overrides_default_weighted_all_source_weight():
    results = [{"chunk_id": "custom"}]
    judgments = [
        {"chunk_id": "custom", "relevance_grade": 3, "source": "llm_assisted", "weight": 0.9},
        {"chunk_id": "missing", "relevance_grade": 3, "source": "gold_manual"},
    ]

    assert precision_at_k(results, judgments, 1) == pytest.approx(0.9)
    assert recall_at_k(results, judgments, 1) == pytest.approx(0.9 / 1.9)


def test_map_at_k_averages_query_average_precision_with_missing_judgment_sets():
    query_results = {
        "q1": [{"chunk_id": "a"}, {"chunk_id": "b"}],
        "q2": [{"chunk_id": "c"}],
        "q3": [{"chunk_id": "d"}],
    }
    query_judgments = {
        "q1": [{"chunk_id": "b", "relevance_grade": 2}],
        "q2": [{"chunk_id": "c", "relevance_grade": 1}],
    }

    assert map_at_k(query_results, query_judgments, k=2) == pytest.approx((0.5 + 0.0 + 0.0) / 3)


def test_stage_recall_returns_per_stage_recall_and_rerank_delta_subtracts_metric_values():
    stage_snapshots = {
        "vector": [{"chunk_id": "a"}],
        "rerank": [{"chunk_id": "b"}, {"chunk_id": "a"}],
        "empty": [],
    }
    judgments = [
        {"chunk_id": "a", "relevance_grade": 2},
        {"chunk_id": "b", "relevance_grade": 3},
    ]

    assert stage_recall_at_k(stage_snapshots, judgments, k=2) == {
        "vector": pytest.approx(0.5),
        "rerank": pytest.approx(1.0),
        "empty": 0.0,
    }
    assert rerank_delta({"recall": 0.5, "latency_ms": 12, "label": "base"}, {"recall": 0.75, "latency_ms": 15}) == {
        "recall": pytest.approx(0.25),
        "latency_ms": pytest.approx(3),
    }
