from __future__ import annotations

import pandas as pd

from tools.trace_dashboard.queries import (
    classify_query_change,
    compare_query_results,
    extract_metric,
    flatten_metrics,
)


def test_extract_metric_reads_common_metric_aliases() -> None:
    metrics = {
        "NDCG@10": 0.72,
        "recall_at_50": 0.91,
        "precision@10": 0.42,
    }

    assert extract_metric(metrics, "ndcg@10") == 0.72
    assert extract_metric(metrics, "Recall@50") == 0.91
    assert extract_metric(metrics, "precision_at_10") == 0.42
    assert extract_metric(metrics, "mrr@50") is None


def test_flatten_metrics_keeps_identifiers_and_expands_metric_payload() -> None:
    rows = pd.DataFrame(
        [
            {
                "eval_query_id": 101,
                "query_text": "如何办理 A？",
                "query_type": "single_doc_rule_or_procedure",
                "trace_id": "trace-a",
                "status": "success",
                "metrics": {
                    "ndcg@10": 0.8,
                    "recall@50": 1.0,
                    "zero_hit": False,
                },
            }
        ]
    )

    out = flatten_metrics(rows)

    assert out.loc[0, "eval_query_id"] == 101
    assert out.loc[0, "query_text"] == "如何办理 A？"
    assert out.loc[0, "ndcg@10"] == 0.8
    assert out.loc[0, "recall@50"] == 1.0
    assert out.loc[0, "zero_hit"] is False


def test_compare_query_results_classifies_improved_regressed_and_missing() -> None:
    baseline = pd.DataFrame(
        [
            {"eval_query_id": 1, "query_text": "Q1", "metrics": {"ndcg@10": 0.4, "recall@50": 0.5}},
            {"eval_query_id": 2, "query_text": "Q2", "metrics": {"ndcg@10": 0.8, "recall@50": 1.0}},
            {"eval_query_id": 3, "query_text": "Q3", "metrics": {"ndcg@10": 0.6, "recall@50": 0.6}},
        ]
    )
    candidate = pd.DataFrame(
        [
            {"eval_query_id": 1, "query_text": "Q1", "metrics": {"ndcg@10": 0.7, "recall@50": 0.8}},
            {"eval_query_id": 2, "query_text": "Q2", "metrics": {"ndcg@10": 0.5, "recall@50": 0.7}},
        ]
    )

    out = compare_query_results(baseline, candidate, metric="ndcg@10", threshold=0.05)

    states = dict(zip(out["eval_query_id"], out["change_type"]))
    deltas = dict(zip(out["eval_query_id"], out["delta"]))
    assert states[1] == "improved"
    assert states[2] == "regressed"
    assert states[3] == "missing_candidate"
    assert round(deltas[1], 3) == 0.3


def test_classify_query_change_uses_threshold() -> None:
    assert classify_query_change(0.10, threshold=0.05) == "improved"
    assert classify_query_change(-0.10, threshold=0.05) == "regressed"
    assert classify_query_change(0.01, threshold=0.05) == "unchanged"
    assert classify_query_change(None, threshold=0.05) == "missing_metric"
