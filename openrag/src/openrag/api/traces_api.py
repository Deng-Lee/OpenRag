"""Trace query API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.models import TraceRun, TraceSnapshot, TraceSpan
from openrag.models.user import User


router = APIRouter(prefix="/traces", tags=["traces"])


@router.get("")
async def list_traces(
    trace_type: Optional[str] = None,
    workspace_id: Optional[int] = None,
    file_id: Optional[int] = None,
    task_id: Optional[str] = None,
    eval_run_id: Optional[int] = None,
    eval_query_id: Optional[int] = None,
    query_hash: Optional[str] = None,
    started_at_from: Optional[datetime] = None,
    started_at_to: Optional[datetime] = None,
    created_at_from: Optional[datetime] = None,
    created_at_to: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List trace runs with indexed filters."""

    query = db.query(TraceRun)
    if not current_user.is_admin:
        query = query.filter(TraceRun.user_id == current_user.id)
    if trace_type is not None:
        query = query.filter(TraceRun.trace_type == trace_type)
    if workspace_id is not None:
        query = query.filter(TraceRun.workspace_id == workspace_id)
    if file_id is not None:
        query = query.filter(TraceRun.file_id == file_id)
    if task_id is not None:
        query = query.filter(TraceRun.task_id == task_id)
    if eval_run_id is not None:
        query = query.filter(TraceRun.eval_run_id == eval_run_id)
    if eval_query_id is not None:
        query = query.filter(TraceRun.eval_query_id == eval_query_id)
    if query_hash is not None:
        query = query.filter(TraceRun.query_hash == query_hash)
    if started_at_from is not None:
        query = query.filter(TraceRun.started_at >= _naive_datetime(started_at_from))
    if started_at_to is not None:
        query = query.filter(TraceRun.started_at <= _naive_datetime(started_at_to))
    if created_at_from is not None:
        query = query.filter(TraceRun.created_at >= _naive_datetime(created_at_from))
    if created_at_to is not None:
        query = query.filter(TraceRun.created_at <= _naive_datetime(created_at_to))

    total = query.count()
    runs = (
        query.order_by(TraceRun.started_at.desc(), TraceRun.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "items": [_trace_run_to_dict(run) for run in runs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{trace_id}/snapshots")
async def get_trace_snapshots(
    trace_id: str,
    stage: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return top50 compact snapshots for a trace, optionally scoped by stage."""

    _get_authorized_trace(trace_id, current_user, db)
    query = db.query(TraceSnapshot).filter(TraceSnapshot.trace_id == trace_id)
    if stage is not None:
        query = query.filter(TraceSnapshot.stage == stage)
    snapshots = query.order_by(TraceSnapshot.stage, TraceSnapshot.rank).limit(50).all()
    return {
        "trace_id": trace_id,
        "stage": stage,
        "items": [_trace_snapshot_to_dict(snapshot) for snapshot in snapshots],
    }


@router.get("/{trace_id}")
async def get_trace(
    trace_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return a trace run with its spans and basic error information."""

    run = _get_authorized_trace(trace_id, current_user, db)
    spans = (
        db.query(TraceSpan)
        .filter(TraceSpan.trace_id == trace_id)
        .order_by(TraceSpan.started_at, TraceSpan.id)
        .all()
    )
    return {
        "run": _trace_run_to_dict(run),
        "spans": [_trace_span_to_dict(span) for span in spans],
        "error": _trace_error(run),
    }


def _get_authorized_trace(trace_id: str, current_user: User, db: Session) -> TraceRun:
    run = db.query(TraceRun).filter(TraceRun.trace_id == trace_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    if not current_user.is_admin and run.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Trace access denied")
    return run


def _trace_run_to_dict(run: TraceRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "trace_id": run.trace_id,
        "trace_type": run.trace_type,
        "workspace_id": run.workspace_id,
        "user_id": run.user_id,
        "file_id": run.file_id,
        "task_id": run.task_id,
        "eval_run_id": run.eval_run_id,
        "eval_query_id": run.eval_query_id,
        "query_hash": run.query_hash,
        "query_preview": run.query_preview,
        "status": run.status,
        "started_at": _datetime_to_iso(run.started_at),
        "ended_at": _datetime_to_iso(run.ended_at),
        "duration_ms": run.duration_ms,
        "error": _trace_error(run),
        "sampling_reason": run.sampling_reason,
        "search_config_snapshot": run.search_config_snapshot,
        "otel_trace_id": run.otel_trace_id,
        "created_at": _datetime_to_iso(run.created_at),
    }


def _trace_span_to_dict(span: TraceSpan) -> dict[str, Any]:
    return {
        "id": span.id,
        "span_id": span.span_id,
        "trace_id": span.trace_id,
        "parent_span_id": span.parent_span_id,
        "stage": span.stage,
        "status": span.status,
        "started_at": _datetime_to_iso(span.started_at),
        "ended_at": _datetime_to_iso(span.ended_at),
        "duration_ms": span.duration_ms,
        "input_summary": span.input_summary,
        "output_summary": span.output_summary,
        "metrics": span.metrics,
        "artifact_refs": span.artifact_refs,
        "error_message": span.error_message,
        "otel_span_id": span.otel_span_id,
        "created_at": _datetime_to_iso(span.created_at),
    }


def _trace_snapshot_to_dict(snapshot: TraceSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "trace_id": snapshot.trace_id,
        "span_id": snapshot.span_id,
        "stage": snapshot.stage,
        "rank": snapshot.rank,
        "chunk_id": snapshot.chunk_id,
        "file_id": snapshot.file_id,
        "score": snapshot.score,
        "score_parts": snapshot.score_parts,
        "metadata": snapshot.metadata_,
        "created_at": _datetime_to_iso(snapshot.created_at),
    }


def _trace_error(run: TraceRun) -> Optional[dict[str, Optional[str]]]:
    if not run.error_stage and not run.error_message:
        return None
    return {"stage": run.error_stage, "message": run.error_message}


def _datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _naive_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(tz=None).replace(tzinfo=None)
