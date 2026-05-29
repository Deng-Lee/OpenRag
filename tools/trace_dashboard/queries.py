from __future__ import annotations

import json
import math
import re
from typing import Any

import pandas as pd
from sqlalchemy.engine import Engine

try:
    from .db import read_df
except ImportError:  # pragma: no cover - Streamlit runs app.py as a script.
    from db import read_df


DISPLAY_METRICS = ["ndcg@10", "recall@50", "precision@10", "mrr@50", "map@50", "zero_hit_rate"]


def _parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return {}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _metric_key(key: str) -> str:
    normalized = key.strip().lower()
    normalized = normalized.replace("precision_at_", "precision@")
    normalized = normalized.replace("recall_at_", "recall@")
    normalized = normalized.replace("hit_rate_at_", "hitrate@")
    normalized = normalized.replace("hit_rate@", "hitrate@")
    normalized = normalized.replace("mrr_at_", "mrr@")
    normalized = normalized.replace("map_at_", "map@")
    normalized = normalized.replace("ndcg_at_", "ndcg@")
    normalized = normalized.replace("_rate", "rate")
    normalized = re.sub(r"[^a-z0-9@]+", "_", normalized).strip("_")
    return normalized


def extract_metric(metrics: dict[str, Any] | str | None, name: str) -> Any:
    payload = _parse_json(metrics)
    wanted = _metric_key(name)
    for key, value in payload.items():
        if _metric_key(str(key)) == wanted:
            return value
    return None


def flatten_metrics(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows.copy()

    flattened: list[dict[str, Any]] = []
    for row in rows.to_dict(orient="records"):
        metrics = _parse_json(row.pop("metrics", {}))
        item = dict(row)
        for key, value in metrics.items():
            item[_metric_key(str(key))] = value
        flattened.append(item)

    out = pd.DataFrame(flattened)
    for column in out.columns:
        if out[column].dtype == bool:
            out[column] = out[column].astype(object)
    return out


def classify_query_change(delta: float | None, threshold: float = 0.01) -> str:
    if delta is None or (isinstance(delta, float) and math.isnan(delta)):
        return "missing_metric"
    if delta >= threshold:
        return "improved"
    if delta <= -threshold:
        return "regressed"
    return "unchanged"


def _value_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric):
        return None
    return numeric


def compare_query_results(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    metric: str = "ndcg@10",
    threshold: float = 0.01,
) -> pd.DataFrame:
    base = flatten_metrics(baseline)
    cand = flatten_metrics(candidate)
    metric_key = _metric_key(metric)

    base["_present_baseline"] = True
    cand["_present_candidate"] = True
    keep = ["eval_query_id", "query_text", "query_type", "trace_id", metric_key]
    merge_keep = keep + ["_present_baseline"]
    candidate_keep = keep + ["_present_candidate"]
    for frame in (base, cand):
        for column in keep:
            if column not in frame.columns:
                frame[column] = None

    merged = base[merge_keep].merge(
        cand[candidate_keep],
        on="eval_query_id",
        how="outer",
        suffixes=("_baseline", "_candidate"),
    )
    merged["query_text"] = merged["query_text_candidate"].fillna(merged["query_text_baseline"])
    merged["query_type"] = merged["query_type_candidate"].fillna(merged["query_type_baseline"])
    merged["baseline_value"] = merged[f"{metric_key}_baseline"].map(_value_or_none)
    merged["candidate_value"] = merged[f"{metric_key}_candidate"].map(_value_or_none)

    def classify(row: pd.Series) -> str:
        if pd.isna(row.get("_present_candidate")):
            return "missing_candidate"
        if pd.isna(row.get("_present_baseline")):
            return "missing_baseline"
        base_value = row["baseline_value"]
        cand_value = row["candidate_value"]
        if base_value is None or cand_value is None:
            return "missing_metric"
        return classify_query_change(cand_value - base_value, threshold)

    merged["delta"] = [
        None if b is None or c is None else c - b
        for b, c in zip(merged["baseline_value"], merged["candidate_value"])
    ]
    merged["change_type"] = merged.apply(classify, axis=1)
    return merged[
        [
            "eval_query_id",
            "query_text",
            "query_type",
            "baseline_value",
            "candidate_value",
            "delta",
            "change_type",
            "trace_id_baseline",
            "trace_id_candidate",
        ]
    ]


def list_workspaces(engine: Engine | None = None) -> pd.DataFrame:
    return read_df("SELECT id, name FROM workspaces ORDER BY name", engine=engine)


def list_datasets(workspace_id: int | None = None, engine: Engine | None = None) -> pd.DataFrame:
    sql = """
        SELECT id, name, workspace_id, status, created_at
        FROM eval_datasets
        WHERE (:workspace_id IS NULL OR workspace_id = :workspace_id)
        ORDER BY created_at DESC
    """
    return read_df(sql, {"workspace_id": workspace_id}, engine)


def list_eval_runs(
    workspace_id: int | None = None,
    dataset_id: int | None = None,
    engine: Engine | None = None,
) -> pd.DataFrame:
    sql = """
        SELECT
            r.id,
            r.dataset_id,
            d.workspace_id,
            d.name AS dataset_name,
            r.name,
            r.status,
            r.index_version,
            r.code_version,
            r.search_config_snapshot,
            r.started_at,
            r.ended_at,
            r.created_at
        FROM eval_runs r
        JOIN eval_datasets d ON d.id = r.dataset_id
        WHERE (:workspace_id IS NULL OR d.workspace_id = :workspace_id)
          AND (:dataset_id IS NULL OR r.dataset_id = :dataset_id)
        ORDER BY r.created_at DESC
    """
    return read_df(sql, {"workspace_id": workspace_id, "dataset_id": dataset_id}, engine)


def get_run_summary(eval_run_id: int, source_scope: str = "weighted_all", engine: Engine | None = None) -> dict[str, Any]:
    sql = """
        SELECT metrics
        FROM eval_results
        WHERE eval_run_id = :eval_run_id
          AND metric_scope = 'run_summary'
          AND source_scope = :source_scope
        ORDER BY created_at DESC
        LIMIT 1
    """
    rows = read_df(sql, {"eval_run_id": eval_run_id, "source_scope": source_scope}, engine)
    if rows.empty:
        return {}
    return _parse_json(rows.iloc[0]["metrics"])


def get_query_results(eval_run_id: int, source_scope: str = "weighted_all", engine: Engine | None = None) -> pd.DataFrame:
    sql = """
        SELECT
            q.id AS eval_query_id,
            q.query_text,
            q.query_type,
            er.trace_id,
            tr.status,
            er.metrics
        FROM eval_results er
        LEFT JOIN eval_queries q ON q.id = er.eval_query_id
        LEFT JOIN trace_runs tr ON tr.trace_id = er.trace_id
        WHERE er.eval_run_id = :eval_run_id
          AND er.metric_scope = 'query'
          AND er.source_scope = :source_scope
        ORDER BY q.id
    """
    return read_df(sql, {"eval_run_id": eval_run_id, "source_scope": source_scope}, engine)


def get_trace_run(trace_id: str, engine: Engine | None = None) -> pd.DataFrame:
    return read_df("SELECT * FROM trace_runs WHERE trace_id = :trace_id", {"trace_id": trace_id}, engine)


def get_trace_spans(trace_id: str, engine: Engine | None = None) -> pd.DataFrame:
    return read_df(
        "SELECT * FROM trace_spans WHERE trace_id = :trace_id ORDER BY started_at, id",
        {"trace_id": trace_id},
        engine,
    )


def get_trace_snapshots(trace_id: str, stage: str | None = None, engine: Engine | None = None) -> pd.DataFrame:
    sql = """
        SELECT id, trace_id, span_id, stage, rank, chunk_id, file_id, score, score_parts, metadata, created_at
        FROM trace_snapshots
        WHERE trace_id = :trace_id
          AND (:stage IS NULL OR stage = :stage)
        ORDER BY stage, rank
    """
    return read_df(sql, {"trace_id": trace_id, "stage": stage}, engine)


def get_eval_judgments(eval_query_id: int, engine: Engine | None = None) -> pd.DataFrame:
    sql = """
        SELECT eval_query_id, chunk_id, file_id, relevance_grade, source, weight, metadata
        FROM eval_judgments
        WHERE eval_query_id = :eval_query_id
        ORDER BY relevance_grade DESC, source
    """
    return read_df(sql, {"eval_query_id": eval_query_id}, engine)
