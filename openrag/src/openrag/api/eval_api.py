"""Retrieval evaluation management API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.models import EvalDataset, EvalJudgment, EvalQuery, EvalResult, EvalRun
from openrag.models.user import User
from openrag.services.eval_service import EvalService


router = APIRouter(prefix="/eval", tags=["eval"])


class DatasetCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    workspace_id: int
    description: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    status: str = "active"


class QueryCreateRequest(BaseModel):
    dataset_id: int
    query_text: str = Field(..., min_length=1)
    query_type: Optional[str] = None
    expected_answer: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class JudgmentCreateRequest(BaseModel):
    dataset_id: int
    eval_query_id: int
    chunk_id: Optional[str] = None
    file_id: Optional[int] = None
    relevance_grade: int = Field(..., ge=0, le=3)
    source: str = "gold_manual"
    weight: Optional[float] = None
    judge_user_id: Optional[int] = None
    judge_model: Optional[str] = None
    judge_reason_ref: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ImportRequest(BaseModel):
    dataset_id: int
    items: list[dict[str, Any]]


class RunCreateRequest(BaseModel):
    dataset_id: int
    search_config_snapshot: Optional[dict[str, Any]] = None
    name: Optional[str] = None
    code_version: Optional[str] = None
    index_version: Optional[str] = None
    generation_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    execute: bool = False


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
async def create_dataset(
    request: DatasetCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = EvalService(db)
    dataset = service.create_dataset(
        name=request.name,
        workspace_id=request.workspace_id,
        description=request.description,
        metadata=request.metadata,
        created_by=current_user.id,
        status=request.status,
    )
    return _dataset_to_dict(dataset)


@router.get("/datasets")
async def list_datasets(
    workspace_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = db.query(EvalDataset)
    if not current_user.is_admin:
        query = query.filter(EvalDataset.created_by == current_user.id)
    if workspace_id is not None:
        query = query.filter(EvalDataset.workspace_id == workspace_id)
    if status is not None:
        query = query.filter(EvalDataset.status == status)
    total = query.count()
    datasets = query.order_by(EvalDataset.created_at, EvalDataset.id).offset(offset).limit(limit).all()
    return {"items": [_dataset_to_dict(dataset) for dataset in datasets], "total": total}


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    dataset = _get_dataset_or_404(dataset_id, current_user, db)
    queries = (
        db.query(EvalQuery)
        .filter(EvalQuery.dataset_id == dataset.id)
        .order_by(EvalQuery.id)
        .all()
    )
    return {
        **_dataset_to_dict(dataset),
        "queries": [_query_to_dict(query) for query in queries],
    }


@router.post("/queries", status_code=status.HTTP_201_CREATED)
async def create_query(
    request: QueryCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_dataset_or_404(request.dataset_id, current_user, db)
    service = EvalService(db)
    eval_query = service.add_query(
        request.dataset_id,
        request.query_text,
        query_type=request.query_type,
        expected_answer=request.expected_answer,
        metadata=request.metadata,
    )
    return _query_to_dict(eval_query)


@router.post("/judgments", status_code=status.HTTP_201_CREATED)
async def create_judgment(
    request: JudgmentCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_dataset_or_404(request.dataset_id, current_user, db)
    service = EvalService(db)
    try:
        judgment = service.add_judgment(
            request.dataset_id,
            request.eval_query_id,
            chunk_id=request.chunk_id,
            file_id=request.file_id,
            relevance_grade=request.relevance_grade,
            source=request.source,
            weight=request.weight,
            judge_user_id=request.judge_user_id or current_user.id,
            judge_model=request.judge_model,
            judge_reason_ref=request.judge_reason_ref,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _judgment_to_dict(judgment)


@router.post("/import")
async def import_dataset(
    request: ImportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    _get_dataset_or_404(request.dataset_id, current_user, db)
    service = EvalService(db)
    return service.import_dataset(request.dataset_id, request.items)


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run(
    request: RunCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_dataset_or_404(request.dataset_id, current_user, db)
    if request.generation_id is not None and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Index generation evaluation requires administrator privileges",
        )
    service = EvalService(db)
    run = service.create_eval_run(
        request.dataset_id,
        request.search_config_snapshot,
        name=request.name,
        code_version=request.code_version,
        index_version=request.index_version,
        generation_id=request.generation_id,
        created_by=current_user.id,
        metadata=request.metadata,
    )
    if request.execute:
        run = service.execute_eval_run(run.id)
    return _run_to_dict(run)


@router.get("/runs")
async def list_runs(
    dataset_id: Optional[int] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = db.query(EvalRun)
    if dataset_id is not None:
        _get_dataset_or_404(dataset_id, current_user, db)
        query = query.filter(EvalRun.dataset_id == dataset_id)
    elif not current_user.is_admin:
        query = query.join(EvalDataset, EvalDataset.id == EvalRun.dataset_id).filter(
            EvalDataset.created_by == current_user.id
        )
    if status_ is not None:
        query = query.filter(EvalRun.status == status_)
    total = query.count()
    runs = query.order_by(EvalRun.created_at.desc(), EvalRun.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_run_to_dict(run) for run in runs], "total": total}


@router.get("/runs/{eval_run_id}/results")
async def get_run_results(
    eval_run_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = _get_run_or_404(eval_run_id, current_user, db)
    results = (
        db.query(EvalResult)
        .filter(EvalResult.eval_run_id == run.id)
        .order_by(EvalResult.metric_scope.desc(), EvalResult.eval_query_id, EvalResult.id)
        .all()
    )
    summary = next((result for result in results if result.metric_scope == "run_summary"), None)
    return {
        "eval_run_id": run.id,
        "items": [_result_to_dict(result) for result in results],
        "summary": _result_to_dict(summary) if summary is not None else None,
    }


@router.get("/runs/{eval_run_id}/compare")
async def compare_runs(
    eval_run_id: int,
    baseline_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    current_run = _get_run_or_404(eval_run_id, current_user, db)
    baseline_run = _get_run_or_404(baseline_id, current_user, db)
    current_summary = _summary_result_or_404(current_run.id, db)
    baseline_summary = _summary_result_or_404(baseline_run.id, db)
    return {
        "current_run_id": current_run.id,
        "baseline_run_id": baseline_run.id,
        "diff": _metrics_diff(current_summary.metrics, baseline_summary.metrics),
    }


@router.get("/runs/{eval_run_id}")
async def get_run(
    eval_run_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = _get_run_or_404(eval_run_id, current_user, db)
    return _run_to_dict(run)


def _get_dataset_or_404(dataset_id: int, current_user: User, db: Session) -> EvalDataset:
    dataset = db.get(EvalDataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Eval dataset not found")
    if not current_user.is_admin and dataset.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Eval dataset access denied")
    return dataset


def _get_run_or_404(eval_run_id: int, current_user: User, db: Session) -> EvalRun:
    run = db.get(EvalRun, eval_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Eval run not found")
    _get_dataset_or_404(run.dataset_id, current_user, db)
    return run


def _summary_result_or_404(eval_run_id: int, db: Session) -> EvalResult:
    result = (
        db.query(EvalResult)
        .filter(EvalResult.eval_run_id == eval_run_id, EvalResult.metric_scope == "run_summary")
        .first()
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Eval run summary not found")
    return result


def _dataset_to_dict(dataset: EvalDataset) -> dict[str, Any]:
    return {
        "id": dataset.id,
        "name": dataset.name,
        "description": dataset.description,
        "workspace_id": dataset.workspace_id,
        "status": dataset.status,
        "metadata": dataset.metadata_,
        "created_by": dataset.created_by,
        "created_at": _datetime_to_iso(dataset.created_at),
        "updated_at": _datetime_to_iso(dataset.updated_at),
    }


def _query_to_dict(eval_query: EvalQuery) -> dict[str, Any]:
    return {
        "id": eval_query.id,
        "dataset_id": eval_query.dataset_id,
        "query_text": eval_query.query_text,
        "query_hash": eval_query.query_hash,
        "query_type": eval_query.query_type,
        "expected_answer": eval_query.expected_answer,
        "metadata": eval_query.metadata_,
        "created_at": _datetime_to_iso(eval_query.created_at),
        "updated_at": _datetime_to_iso(eval_query.updated_at),
    }


def _judgment_to_dict(judgment: EvalJudgment) -> dict[str, Any]:
    return {
        "id": judgment.id,
        "dataset_id": judgment.dataset_id,
        "eval_query_id": judgment.eval_query_id,
        "chunk_id": judgment.chunk_id,
        "file_id": judgment.file_id,
        "relevance_grade": judgment.relevance_grade,
        "source": judgment.source,
        "weight": judgment.weight,
        "judge_user_id": judgment.judge_user_id,
        "judge_model": judgment.judge_model,
        "judge_reason_ref": judgment.judge_reason_ref,
        "metadata": judgment.metadata_,
        "created_at": _datetime_to_iso(judgment.created_at),
        "updated_at": _datetime_to_iso(judgment.updated_at),
    }


def _run_to_dict(run: EvalRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "dataset_id": run.dataset_id,
        "name": run.name,
        "status": run.status,
        "search_config_snapshot": run.search_config_snapshot,
        "code_version": run.code_version,
        "index_version": run.index_version,
        "started_at": _datetime_to_iso(run.started_at),
        "ended_at": _datetime_to_iso(run.ended_at),
        "created_by": run.created_by,
        "metadata": run.metadata_,
        "created_at": _datetime_to_iso(run.created_at),
    }


def _result_to_dict(result: EvalResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "eval_run_id": result.eval_run_id,
        "eval_query_id": result.eval_query_id,
        "trace_id": result.trace_id,
        "metric_scope": result.metric_scope,
        "source_scope": result.source_scope,
        "metrics": result.metrics,
        "created_at": _datetime_to_iso(result.created_at),
    }


def _metrics_diff(
    current_metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    keys = sorted(set(current_metrics) | set(baseline_metrics))
    diff: dict[str, dict[str, Any]] = {}
    for key in keys:
        current = current_metrics.get(key)
        baseline = baseline_metrics.get(key)
        delta = None
        if _is_number(current) and _is_number(baseline):
            delta = float(current) - float(baseline)
        diff[key] = {"current": current, "baseline": baseline, "delta": delta}
    return diff


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()
