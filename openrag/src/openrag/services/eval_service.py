"""Evaluation dataset and synchronous eval run execution service."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from openrag.evaluation.metrics import (
    average_precision_at_k,
    hit_rate_at_k,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    stage_recall_at_k,
)
from openrag.models import (
    EvalDataset,
    EvalJudgment,
    EvalQuery,
    EvalResult,
    EvalRun,
)
from openrag.services.trace_service import TraceService
from openrag.tracing.context import reset_trace_context


SearchCallable = Callable[..., Any]


class EvalService:
    """Manage retrieval evaluation datasets and synchronous eval runs."""

    def __init__(self, db: Session, search_callable: Optional[SearchCallable] = None):
        self.db = db
        self.search_callable = search_callable or self._default_search

    def create_dataset(
        self,
        *,
        name: str,
        workspace_id: int,
        description: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        created_by: Optional[int] = None,
        status: str = "active",
    ) -> EvalDataset:
        dataset = EvalDataset(
            name=name,
            workspace_id=workspace_id,
            description=description,
            metadata_=metadata,
            created_by=created_by,
            status=status,
        )
        return self._commit_new(dataset)

    def list_datasets(
        self,
        *,
        workspace_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> list[EvalDataset]:
        query = self.db.query(EvalDataset)
        if workspace_id is not None:
            query = query.filter(EvalDataset.workspace_id == workspace_id)
        if status is not None:
            query = query.filter(EvalDataset.status == status)
        return query.order_by(EvalDataset.created_at, EvalDataset.id).all()

    def get_dataset(self, dataset_id: int) -> EvalDataset:
        dataset = self.db.get(EvalDataset, dataset_id)
        if dataset is None:
            raise ValueError(f"Eval dataset not found: {dataset_id}")
        return dataset

    def add_query(
        self,
        dataset_id: int,
        query_text: str,
        *,
        query_type: Optional[str] = None,
        expected_answer: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> EvalQuery:
        self.get_dataset(dataset_id)
        eval_query = EvalQuery(
            dataset_id=dataset_id,
            query_text=query_text,
            query_hash=_query_hash(query_text),
            query_type=query_type,
            expected_answer=expected_answer,
            metadata_=metadata,
        )
        return self._commit_new(eval_query)

    def add_judgment(
        self,
        dataset_id: int,
        eval_query_id: int,
        *,
        chunk_id: Optional[str] = None,
        file_id: Optional[int] = None,
        relevance_grade: int,
        source: str = "gold_manual",
        weight: Optional[float] = None,
        judge_user_id: Optional[int] = None,
        judge_model: Optional[str] = None,
        judge_reason_ref: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> EvalJudgment:
        self.get_dataset(dataset_id)
        eval_query = self.db.get(EvalQuery, eval_query_id)
        if eval_query is None or eval_query.dataset_id != dataset_id:
            raise ValueError(f"Eval query not found in dataset: {eval_query_id}")
        judgment = EvalJudgment(
            dataset_id=dataset_id,
            eval_query_id=eval_query_id,
            chunk_id=chunk_id,
            file_id=file_id,
            relevance_grade=relevance_grade,
            source=source,
            weight=weight,
            judge_user_id=judge_user_id,
            judge_model=judge_model,
            judge_reason_ref=judge_reason_ref,
            metadata_=metadata,
        )
        return self._commit_new(judgment)

    def import_dataset(
        self,
        dataset_id: int,
        items: Iterable[Mapping[str, Any]],
    ) -> dict[str, int]:
        query_count = 0
        judgment_count = 0
        for item in items:
            eval_query = self.add_query(
                dataset_id,
                str(item["query_text"]),
                query_type=item.get("query_type"),
                expected_answer=item.get("expected_answer"),
                metadata=item.get("metadata"),
            )
            query_count += 1
            for judgment in item.get("judgments", []):
                self.add_judgment(
                    dataset_id,
                    eval_query.id,
                    chunk_id=judgment.get("chunk_id"),
                    file_id=judgment.get("file_id"),
                    relevance_grade=int(judgment["relevance_grade"]),
                    source=judgment.get("source", "gold_manual"),
                    weight=judgment.get("weight"),
                    judge_user_id=judgment.get("judge_user_id"),
                    judge_model=judgment.get("judge_model"),
                    judge_reason_ref=judgment.get("judge_reason_ref"),
                    metadata=judgment.get("metadata"),
                )
                judgment_count += 1
        return {"query_count": query_count, "judgment_count": judgment_count}

    def create_eval_run(
        self,
        dataset_id: int,
        search_config_snapshot: Optional[dict[str, Any]],
        *,
        name: Optional[str] = None,
        code_version: Optional[str] = None,
        index_version: Optional[str] = None,
        created_by: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> EvalRun:
        self.get_dataset(dataset_id)
        run = EvalRun(
            dataset_id=dataset_id,
            name=name,
            status="pending",
            search_config_snapshot=search_config_snapshot or {},
            code_version=code_version,
            index_version=index_version,
            created_by=created_by,
            metadata_=metadata,
        )
        return self._commit_new(run)

    def execute_eval_run(self, eval_run_id: int) -> EvalRun:
        run = self._get_eval_run(eval_run_id)
        dataset = self.get_dataset(run.dataset_id)
        queries = (
            self.db.query(EvalQuery)
            .filter(EvalQuery.dataset_id == dataset.id)
            .order_by(EvalQuery.id)
            .all()
        )

        self._delete_existing_results(run.id)
        run.status = "running"
        run.started_at = _utcnow_naive()
        run.ended_at = None
        self.db.commit()

        for eval_query in queries:
            self._execute_query(run, dataset, eval_query)

        self.summarize_eval_run(run.id)
        run.status = "success"
        run.ended_at = _utcnow_naive()
        self.db.commit()
        self.db.refresh(run)
        reset_trace_context()
        return run

    def summarize_eval_run(self, eval_run_id: int) -> EvalResult:
        run = self._get_eval_run(eval_run_id)
        query_results = (
            self.db.query(EvalResult)
            .filter(EvalResult.eval_run_id == run.id, EvalResult.metric_scope == "query")
            .order_by(EvalResult.eval_query_id)
            .all()
        )
        failed_count = sum(1 for result in query_results if result.metrics.get("status") == "failed")
        successful_metrics = [
            result.metrics
            for result in query_results
            if result.metrics.get("status") != "failed"
        ]
        summary = _average_numeric_metrics(successful_metrics)
        summary.update(
            {
                "status": "success",
                "query_count": len(query_results),
                "success_count": len(successful_metrics),
                "failed_count": failed_count,
            }
        )

        (
            self.db.query(EvalResult)
            .filter(EvalResult.eval_run_id == run.id, EvalResult.metric_scope == "run_summary")
            .delete(synchronize_session=False)
        )
        result = EvalResult(
            eval_run_id=run.id,
            eval_query_id=None,
            trace_id=None,
            metric_scope="run_summary",
            source_scope=_source_scope(run.search_config_snapshot),
            metrics=summary,
        )
        return self._commit_new(result)

    def _execute_query(
        self,
        run: EvalRun,
        dataset: EvalDataset,
        eval_query: EvalQuery,
    ) -> None:
        trace_service = TraceService(self.db)
        config = dict(run.search_config_snapshot or {})
        top_k = int(config.get("top_k", 10))
        source_scope = _source_scope(config)
        trace_id = uuid.uuid4().hex
        trace_service.start_run(
            trace_type="eval_retrieval",
            trace_id=trace_id,
            workspace_id=dataset.workspace_id,
            user_id=run.created_by,
            eval_run_id=run.id,
            eval_query_id=eval_query.id,
            query_hash=eval_query.query_hash,
            query_preview=eval_query.query_text[:512],
            sampling_reason="eval_run",
            search_config_snapshot=config,
        )
        trace_service.start_span(
            "eval.search",
            input_summary={"eval_query_id": eval_query.id, "top_k": top_k},
        )

        try:
            raw_output = self.search_callable(
                query=eval_query.query_text,
                user_id=run.created_by or 0,
                workspace_id=dataset.workspace_id,
                **config,
            )
            results, stage_snapshots = _normalize_search_output(raw_output)
            if not stage_snapshots:
                stage_snapshots = {"final": results}
            self._record_stage_snapshots(trace_service, stage_snapshots)
            judgments = self._judgments_for_query(eval_query.id)
            metrics = _query_metrics(
                results,
                judgments,
                stage_snapshots,
                top_k=top_k,
                source_scope=source_scope,
            )
            metrics.update({"status": "success", "result_count": len(results)})
            trace_service.finish_span(
                output_summary={"result_count": len(results)},
                metrics=metrics,
            )
            trace_service.finish_run(trace_id=trace_id)
            self._save_query_result(run, eval_query, trace_id, source_scope, metrics)
        except Exception as exc:
            trace_service.fail_span(error_message=str(exc))
            trace_service.fail_run(
                trace_id=trace_id,
                error_stage="eval.search",
                error_message=str(exc),
            )
            self._save_query_result(
                run,
                eval_query,
                trace_id,
                source_scope,
                {"status": "failed", "error": str(exc)},
            )

    def _record_stage_snapshots(
        self,
        trace_service: TraceService,
        stage_snapshots: Mapping[str, Sequence[Any]],
    ) -> None:
        for stage, items in stage_snapshots.items():
            for rank, item in enumerate(items, start=1):
                trace_service.record_snapshot(
                    stage=str(stage),
                    rank=rank,
                    chunk_id=_read_field(item, "chunk_id"),
                    file_id=_read_optional_int(item, "file_id"),
                    score=_read_optional_float(item, "score"),
                    score_parts=_read_mapping(item, "score_parts"),
                    metadata=_snapshot_metadata(item),
                )

    def _judgments_for_query(self, eval_query_id: int) -> list[EvalJudgment]:
        return (
            self.db.query(EvalJudgment)
            .filter(EvalJudgment.eval_query_id == eval_query_id)
            .order_by(EvalJudgment.id)
            .all()
        )

    def _save_query_result(
        self,
        run: EvalRun,
        eval_query: EvalQuery,
        trace_id: str,
        source_scope: str,
        metrics: dict[str, Any],
    ) -> EvalResult:
        result = EvalResult(
            eval_run_id=run.id,
            eval_query_id=eval_query.id,
            trace_id=trace_id,
            metric_scope="query",
            source_scope=source_scope,
            metrics=metrics,
        )
        return self._commit_new(result)

    def _delete_existing_results(self, eval_run_id: int) -> None:
        (
            self.db.query(EvalResult)
            .filter(EvalResult.eval_run_id == eval_run_id)
            .delete(synchronize_session=False)
        )
        self.db.commit()

    def _get_eval_run(self, eval_run_id: int) -> EvalRun:
        run = self.db.get(EvalRun, eval_run_id)
        if run is None:
            raise ValueError(f"Eval run not found: {eval_run_id}")
        return run

    def _commit_new(self, obj: Any) -> Any:
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def _default_search(self, *, query: str, user_id: int, workspace_id: int, **config: Any) -> list[dict[str, Any]]:
        from openrag.api.search_api import SearchRequest, _execute_search

        fields = getattr(SearchRequest, "model_fields", getattr(SearchRequest, "__fields__", {}))
        request_kwargs = {
            key: value
            for key, value in config.items()
            if key in fields and key not in {"query", "workspace_id"}
        }
        request = SearchRequest(query=query, workspace_id=workspace_id, **request_kwargs)
        endpoint = str(config.get("endpoint", "semantic"))
        response = _execute_search(
            self.db,
            user_id,
            request,
            endpoint=endpoint,
            rerank_hierarchical_boost=0.15 if endpoint == "hierarchical" else None,
        )
        return [_model_to_dict(result) for result in response.results]


def _query_hash(query_text: str) -> str:
    normalized = " ".join(query_text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _query_metrics(
    results: Sequence[Any],
    judgments: Sequence[Any],
    stage_snapshots: Mapping[str, Sequence[Any]],
    *,
    top_k: int,
    source_scope: str,
) -> dict[str, Any]:
    return {
        f"precision@{top_k}": precision_at_k(results, judgments, top_k, source_scope=source_scope),
        f"recall@{top_k}": recall_at_k(results, judgments, top_k, source_scope=source_scope),
        f"hit_rate@{top_k}": hit_rate_at_k(results, judgments, top_k, source_scope=source_scope),
        "mrr@50": mrr_at_k(results, judgments, k=50, source_scope=source_scope),
        "map@50": average_precision_at_k(results, judgments, k=50, source_scope=source_scope),
        f"ndcg@{top_k}": ndcg_at_k(results, judgments, top_k, source_scope=source_scope),
        "stage_recall@50": stage_recall_at_k(stage_snapshots, judgments, k=50, source_scope=source_scope),
    }


def _average_numeric_metrics(metrics_list: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not metrics_list:
        return {}
    keys = sorted({key for metrics in metrics_list for key in metrics})
    averages: dict[str, Any] = {}
    for key in keys:
        values = [
            metrics[key]
            for metrics in metrics_list
            if isinstance(metrics.get(key), (int, float)) and not isinstance(metrics.get(key), bool)
        ]
        if values:
            averages[key] = sum(float(value) for value in values) / len(values)
    return averages


def _normalize_search_output(raw_output: Any) -> tuple[list[Any], dict[str, list[Any]]]:
    if isinstance(raw_output, Mapping):
        results = list(raw_output.get("results", []))
        raw_snapshots = raw_output.get("stage_snapshots") or raw_output.get("snapshots") or {}
        stage_snapshots = {
            str(stage): list(items)
            for stage, items in raw_snapshots.items()
        }
        return results, stage_snapshots
    return list(raw_output or []), {}


def _source_scope(config: Optional[Mapping[str, Any]]) -> str:
    if not config:
        return "weighted_all"
    return str(config.get("source_scope", "weighted_all"))


def _read_field(item: Any, name: str, default: Any = None) -> Any:
    if item is None:
        return default
    if isinstance(item, Mapping):
        return item.get(name, default)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump().get(name, default)
    dict_method = getattr(item, "dict", None)
    if callable(dict_method):
        return dict_method().get(name, default)
    return getattr(item, name, default)


def _read_optional_int(item: Any, name: str) -> Optional[int]:
    value = _read_field(item, name)
    if value in (None, ""):
        return None
    return int(value)


def _read_optional_float(item: Any, name: str) -> Optional[float]:
    value = _read_field(item, name)
    if value in (None, ""):
        return None
    return float(value)


def _read_mapping(item: Any, name: str) -> Optional[dict[str, Any]]:
    value = _read_field(item, name)
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _snapshot_metadata(item: Any) -> Optional[dict[str, Any]]:
    metadata = _read_mapping(item, "metadata")
    if metadata is not None:
        return metadata
    if isinstance(item, Mapping):
        excluded = {"chunk_id", "file_id", "score", "score_parts"}
        extra = {str(key): value for key, value in item.items() if key not in excluded}
        return extra or None
    return None


def _model_to_dict(item: Any) -> dict[str, Any]:
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    dict_method = getattr(item, "dict", None)
    if callable(dict_method):
        return dict_method()
    if isinstance(item, Mapping):
        return dict(item)
    return dict(vars(item))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
