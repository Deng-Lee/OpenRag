"""Retrieval quality metrics for eval runs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, is_dataclass
import math
from numbers import Real
from typing import Any, Iterable, Mapping, Sequence


SOURCE_WEIGHTS = {
    "gold_manual": 1.0,
    "business_import": 0.7,
    "llm_assisted": 0.4,
}
SOURCE_SCOPES = {*SOURCE_WEIGHTS, "weighted_all"}
RELEVANT_GRADE = 2


def precision_at_k(
    results: Iterable[Any],
    judgments: Iterable[Any],
    k: int,
    source_scope: str = "weighted_all",
) -> float:
    """Return weighted precision@k using relevance_grade >= 2 as relevant."""

    if k <= 0:
        return 0.0
    relevant = _binary_relevance_by_chunk(judgments, source_scope)
    if not relevant:
        return 0.0

    seen: set[str] = set()
    gain = 0.0
    for result in _top_k(results, k):
        chunk_key = _item_key(result)
        if chunk_key is None or chunk_key in seen:
            continue
        seen.add(chunk_key)
        gain += relevant.get(chunk_key, 0.0)
    return gain / k


def recall_at_k(
    results: Iterable[Any],
    judgments: Iterable[Any],
    k: int,
    source_scope: str = "weighted_all",
) -> float:
    """Return weighted recall@k using relevance_grade >= 2 as relevant."""

    if k <= 0:
        return 0.0
    relevant = _binary_relevance_by_chunk(judgments, source_scope)
    total_relevant = sum(relevant.values())
    if total_relevant <= 0:
        return 0.0

    seen: set[str] = set()
    gain = 0.0
    for result in _top_k(results, k):
        chunk_key = _item_key(result)
        if chunk_key is None or chunk_key in seen:
            continue
        seen.add(chunk_key)
        gain += relevant.get(chunk_key, 0.0)
    return gain / total_relevant


def hit_rate_at_k(
    results: Iterable[Any],
    judgments: Iterable[Any],
    k: int,
    source_scope: str = "weighted_all",
) -> float:
    """Return 1.0 when at least one relevant chunk appears in top k."""

    if k <= 0:
        return 0.0
    relevant = _binary_relevance_by_chunk(judgments, source_scope)
    if not relevant:
        return 0.0

    seen: set[str] = set()
    for result in _top_k(results, k):
        chunk_key = _item_key(result)
        if chunk_key is None or chunk_key in seen:
            continue
        seen.add(chunk_key)
        if relevant.get(chunk_key, 0.0) > 0:
            return 1.0
    return 0.0


def mrr_at_k(
    results: Iterable[Any],
    judgments: Iterable[Any],
    k: int = 50,
    source_scope: str = "weighted_all",
) -> float:
    """Return reciprocal rank of the first relevant unique chunk."""

    if k <= 0:
        return 0.0
    relevant = _binary_relevance_by_chunk(judgments, source_scope)
    if not relevant:
        return 0.0

    seen: set[str] = set()
    for rank, result in enumerate(_top_k(results, k), start=1):
        chunk_key = _item_key(result)
        if chunk_key is None or chunk_key in seen:
            continue
        seen.add(chunk_key)
        if relevant.get(chunk_key, 0.0) > 0:
            return 1.0 / rank
    return 0.0


def average_precision_at_k(
    results: Iterable[Any],
    judgments: Iterable[Any],
    k: int = 50,
    source_scope: str = "weighted_all",
) -> float:
    """Return weighted average precision@k."""

    if k <= 0:
        return 0.0
    relevant = _binary_relevance_by_chunk(judgments, source_scope)
    total_relevant = sum(relevant.values())
    if total_relevant <= 0:
        return 0.0

    seen: set[str] = set()
    cumulative_gain = 0.0
    precision_sum = 0.0
    for rank, result in enumerate(_top_k(results, k), start=1):
        chunk_key = _item_key(result)
        if chunk_key is None or chunk_key in seen:
            continue
        seen.add(chunk_key)
        hit_gain = relevant.get(chunk_key, 0.0)
        if hit_gain <= 0:
            continue
        cumulative_gain += hit_gain
        precision_sum += (cumulative_gain / rank) * hit_gain
    return precision_sum / total_relevant


def map_at_k(
    query_results: Mapping[Any, Iterable[Any]] | Sequence[Iterable[Any]],
    query_judgments: Mapping[Any, Iterable[Any]] | Sequence[Iterable[Any]],
    k: int = 50,
    source_scope: str = "weighted_all",
) -> float:
    """Return mean average precision@k across queries."""

    query_pairs = list(_iter_query_pairs(query_results, query_judgments))
    if not query_pairs:
        return 0.0
    total = sum(
        average_precision_at_k(results, judgments, k=k, source_scope=source_scope)
        for results, judgments in query_pairs
    )
    return total / len(query_pairs)


def ndcg_at_k(
    results: Iterable[Any],
    judgments: Iterable[Any],
    k: int,
    source_scope: str = "weighted_all",
) -> float:
    """Return nDCG@k using raw 0-3 judgment grades as gains."""

    if k <= 0:
        return 0.0
    gains = _graded_relevance_by_chunk(judgments, source_scope)
    if not gains:
        return 0.0

    seen: set[str] = set()
    ranked_gains: list[float] = []
    for result in _top_k(results, k):
        chunk_key = _item_key(result)
        if chunk_key is None or chunk_key in seen:
            ranked_gains.append(0.0)
            continue
        seen.add(chunk_key)
        ranked_gains.append(gains.get(chunk_key, 0.0))

    dcg = _dcg(ranked_gains[:k])
    ideal_dcg = _dcg(sorted(gains.values(), reverse=True)[:k])
    if ideal_dcg <= 0:
        return 0.0
    return dcg / ideal_dcg


def stage_recall_at_k(
    stage_snapshots: Mapping[str, Iterable[Any]] | Iterable[Any],
    judgments: Iterable[Any],
    k: int = 50,
    source_scope: str = "weighted_all",
) -> dict[str, float]:
    """Return recall@k for each retrieval stage snapshot."""

    if isinstance(stage_snapshots, Mapping):
        stages = stage_snapshots.items()
    else:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for snapshot in stage_snapshots:
            stage = _read_field(snapshot, "stage")
            if stage is not None:
                grouped[str(stage)].append(snapshot)
        stages = grouped.items()
    return {
        str(stage): recall_at_k(results, judgments, k=k, source_scope=source_scope)
        for stage, results in stages
    }


def rerank_delta(before_metrics: Mapping[str, Any], after_metrics: Mapping[str, Any]) -> dict[str, float]:
    """Return numeric metric deltas between after and before reranking."""

    delta: dict[str, float] = {}
    for key, before_value in before_metrics.items():
        if key not in after_metrics:
            continue
        after_value = after_metrics[key]
        if _is_number(before_value) and _is_number(after_value):
            delta[str(key)] = float(after_value) - float(before_value)
    return delta


def _binary_relevance_by_chunk(judgments: Iterable[Any], source_scope: str) -> dict[str, float]:
    _validate_source_scope(source_scope)
    relevance: dict[str, float] = defaultdict(float)
    for judgment in judgments:
        if not _include_judgment(judgment, source_scope):
            continue
        chunk_key = _item_key(judgment)
        if chunk_key is None:
            continue
        grade = _read_int(judgment, "relevance_grade", 0)
        if grade < RELEVANT_GRADE:
            continue
        relevance[chunk_key] += _judgment_weight(judgment, source_scope)
    return dict(relevance)


def _graded_relevance_by_chunk(judgments: Iterable[Any], source_scope: str) -> dict[str, float]:
    _validate_source_scope(source_scope)
    relevance: dict[str, float] = defaultdict(float)
    for judgment in judgments:
        if not _include_judgment(judgment, source_scope):
            continue
        chunk_key = _item_key(judgment)
        if chunk_key is None:
            continue
        grade = max(0, min(3, _read_int(judgment, "relevance_grade", 0)))
        if grade <= 0:
            continue
        relevance[chunk_key] += grade * _judgment_weight(judgment, source_scope)
    return dict(relevance)


def _include_judgment(judgment: Any, source_scope: str) -> bool:
    if source_scope == "weighted_all":
        return _read_field(judgment, "source", "gold_manual") in SOURCE_WEIGHTS
    return _read_field(judgment, "source", "gold_manual") == source_scope


def _judgment_weight(judgment: Any, source_scope: str) -> float:
    if source_scope != "weighted_all":
        return 1.0
    explicit_weight = _read_field(judgment, "weight")
    if explicit_weight is not None:
        return float(explicit_weight)
    source = _read_field(judgment, "source", "gold_manual")
    return SOURCE_WEIGHTS.get(source, 0.0)


def _item_key(item: Any) -> str | None:
    chunk_id = _read_field(item, "chunk_id")
    if chunk_id not in (None, ""):
        return f"chunk:{chunk_id}"
    file_id = _read_field(item, "file_id")
    if file_id not in (None, ""):
        return f"file:{file_id}"
    return None


def _read_field(item: Any, name: str, default: Any = None) -> Any:
    if item is None:
        return default
    if isinstance(item, Mapping):
        return item.get(name, default)
    if is_dataclass(item):
        return asdict(item).get(name, default)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump().get(name, default)
    dict_method = getattr(item, "dict", None)
    if callable(dict_method):
        return dict_method().get(name, default)
    return getattr(item, name, default)


def _read_int(item: Any, name: str, default: int) -> int:
    value = _read_field(item, name, default)
    if value is None:
        return default
    return int(value)


def _top_k(items: Iterable[Any], k: int) -> list[Any]:
    if k <= 0:
        return []
    result: list[Any] = []
    for item in items:
        result.append(item)
        if len(result) >= k:
            break
    return result


def _iter_query_pairs(
    query_results: Mapping[Any, Iterable[Any]] | Sequence[Iterable[Any]],
    query_judgments: Mapping[Any, Iterable[Any]] | Sequence[Iterable[Any]],
) -> Iterable[tuple[Iterable[Any], Iterable[Any]]]:
    if isinstance(query_results, Mapping):
        if isinstance(query_judgments, Mapping):
            for query_id, results in query_results.items():
                yield results, query_judgments.get(query_id, [])
        else:
            judgment_list = list(query_judgments)
            for index, results in enumerate(query_results.values()):
                yield results, judgment_list[index] if index < len(judgment_list) else []
        return

    result_list = list(query_results)
    if isinstance(query_judgments, Mapping):
        judgment_values = list(query_judgments.values())
    else:
        judgment_values = list(query_judgments)
    for index, results in enumerate(result_list):
        yield results, judgment_values[index] if index < len(judgment_values) else []


def _dcg(gains: Sequence[float]) -> float:
    return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _validate_source_scope(source_scope: str) -> None:
    if source_scope not in SOURCE_SCOPES:
        raise ValueError(f"Unsupported source_scope: {source_scope}")
