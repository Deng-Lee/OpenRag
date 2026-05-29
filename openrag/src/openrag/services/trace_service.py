"""Best-effort persistence for trace runs, spans, snapshots, and artifacts."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from openrag.models import TraceArtifact, TraceRun, TraceSnapshot, TraceSpan
from openrag.tracing.context import (
    get_trace_context,
    pop_span,
    push_span,
    set_trace_context,
)

logger = logging.getLogger(__name__)


class TraceService:
    """Persist trace data without letting tracing failures affect callers."""

    def __init__(self, db: Session):
        self.db = db

    def start_run(
        self,
        *,
        trace_type: Optional[str] = None,
        trace_id: Optional[str] = None,
        workspace_id: Optional[int] = None,
        user_id: Optional[int] = None,
        file_id: Optional[int] = None,
        task_id: Optional[str] = None,
        eval_run_id: Optional[int] = None,
        eval_query_id: Optional[int] = None,
        query_hash: Optional[str] = None,
        query_preview: Optional[str] = None,
        sampling_reason: Optional[str] = None,
        search_config_snapshot: Optional[Dict[str, Any]] = None,
        otel_trace_id: Optional[str] = None,
    ) -> Optional[TraceRun]:
        ctx = get_trace_context()
        resolved_trace_id = trace_id or ctx["trace_id"] or uuid.uuid4().hex
        resolved_trace_type = trace_type or ctx["trace_type"] or "unknown"
        set_trace_context(
            trace_id=resolved_trace_id,
            trace_type=resolved_trace_type,
            workspace_id=workspace_id if workspace_id is not None else ctx["workspace_id"],
            user_id=user_id if user_id is not None else ctx["user_id"],
            file_id=file_id if file_id is not None else ctx["file_id"],
            task_id=task_id if task_id is not None else ctx["task_id"],
            eval_run_id=eval_run_id if eval_run_id is not None else ctx["eval_run_id"],
            eval_query_id=eval_query_id if eval_query_id is not None else ctx["eval_query_id"],
            sampling_reason=(
                sampling_reason if sampling_reason is not None else ctx["sampling_reason"]
            ),
        )
        ctx = get_trace_context()
        run = TraceRun(
            trace_id=resolved_trace_id,
            trace_type=resolved_trace_type,
            workspace_id=ctx["workspace_id"],
            user_id=ctx["user_id"],
            file_id=ctx["file_id"],
            task_id=ctx["task_id"],
            eval_run_id=ctx["eval_run_id"],
            eval_query_id=ctx["eval_query_id"],
            query_hash=query_hash,
            query_preview=query_preview,
            status="running",
            started_at=_utcnow_naive(),
            sampling_reason=ctx["sampling_reason"],
            search_config_snapshot=search_config_snapshot,
            otel_trace_id=otel_trace_id,
        )
        return self._commit_new(run)

    def finish_run(self, *, trace_id: Optional[str] = None) -> Optional[TraceRun]:
        return self._complete_run(trace_id=trace_id, status="success")

    def fail_run(
        self,
        *,
        trace_id: Optional[str] = None,
        error_stage: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Optional[TraceRun]:
        return self._complete_run(
            trace_id=trace_id,
            status="failed",
            error_stage=error_stage,
            error_message=error_message,
        )

    def start_span(
        self,
        stage: str,
        *,
        span_id: Optional[str] = None,
        input_summary: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        artifact_refs: Optional[Dict[str, Any]] = None,
        otel_span_id: Optional[str] = None,
    ) -> Optional[TraceSpan]:
        ctx = get_trace_context()
        trace_id = ctx["trace_id"]
        if not trace_id:
            logger.warning("TraceService write skipped: missing trace_id for span %s", stage)
            return None
        parent_span_id = ctx["span_id"]
        active_span_id = push_span(span_id)
        span = TraceSpan(
            span_id=active_span_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            stage=stage,
            status="running",
            started_at=_utcnow_naive(),
            input_summary=input_summary,
            metrics=metrics,
            artifact_refs=artifact_refs,
            otel_span_id=otel_span_id,
        )
        return self._commit_new(span)

    def finish_span(
        self,
        *,
        span_id: Optional[str] = None,
        output_summary: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        artifact_refs: Optional[Dict[str, Any]] = None,
    ) -> Optional[TraceSpan]:
        span = self._complete_span(
            span_id=span_id,
            status="success",
            output_summary=output_summary,
            metrics=metrics,
            artifact_refs=artifact_refs,
        )
        if span_id is None or span_id == get_trace_context()["span_id"]:
            pop_span()
        return span

    def fail_span(
        self,
        *,
        span_id: Optional[str] = None,
        error_message: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Optional[TraceSpan]:
        span = self._complete_span(
            span_id=span_id,
            status="failed",
            error_message=error_message,
            metrics=metrics,
        )
        if span_id is None or span_id == get_trace_context()["span_id"]:
            pop_span()
        return span

    def record_snapshot(
        self,
        *,
        stage: str,
        rank: int,
        chunk_id: Optional[str] = None,
        file_id: Optional[int] = None,
        score: Optional[float] = None,
        score_parts: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> Optional[TraceSnapshot]:
        ctx = get_trace_context()
        resolved_trace_id = trace_id or ctx["trace_id"]
        if not resolved_trace_id:
            logger.warning("TraceService write skipped: missing trace_id for snapshot %s", stage)
            return None
        snapshot = TraceSnapshot(
            trace_id=resolved_trace_id,
            span_id=span_id if span_id is not None else ctx["span_id"],
            stage=stage,
            rank=rank,
            chunk_id=chunk_id,
            file_id=file_id,
            score=score,
            score_parts=score_parts,
            metadata_=metadata,
        )
        return self._commit_new(snapshot)

    def record_artifact_ref(
        self,
        *,
        artifact_type: str,
        bucket: str,
        object_key: str,
        storage_backend: str = "minio",
        content_type: Optional[str] = None,
        size_bytes: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        expires_at: Optional[datetime] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> Optional[TraceArtifact]:
        ctx = get_trace_context()
        resolved_trace_id = trace_id or ctx["trace_id"]
        if not resolved_trace_id:
            logger.warning(
                "TraceService write skipped: missing trace_id for artifact %s",
                artifact_type,
            )
            return None
        artifact = TraceArtifact(
            artifact_id=uuid.uuid4().hex,
            trace_id=resolved_trace_id,
            span_id=span_id if span_id is not None else ctx["span_id"],
            artifact_type=artifact_type,
            storage_backend=storage_backend,
            bucket=bucket,
            object_key=object_key,
            content_type=content_type,
            size_bytes=size_bytes,
            metadata_=metadata,
            expires_at=expires_at or (_utcnow_naive() + timedelta(days=7)),
        )
        return self._commit_new(artifact)

    def _complete_run(
        self,
        *,
        trace_id: Optional[str],
        status: str,
        error_stage: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Optional[TraceRun]:
        resolved_trace_id = trace_id or get_trace_context()["trace_id"]
        if not resolved_trace_id:
            logger.warning("TraceService write skipped: missing trace_id for run completion")
            return None
        try:
            run = self.db.query(TraceRun).filter(TraceRun.trace_id == resolved_trace_id).first()
            if run is None:
                logger.warning("TraceService write skipped: trace run %s not found", resolved_trace_id)
                return None
            ended_at = _utcnow_naive()
            run.status = status
            run.ended_at = ended_at
            run.duration_ms = _duration_ms(run.started_at, ended_at)
            run.error_stage = error_stage
            run.error_message = error_message
            self.db.commit()
            self.db.refresh(run)
            return run
        except Exception as exc:
            self._rollback_warning(exc)
            return None

    def _complete_span(
        self,
        *,
        span_id: Optional[str],
        status: str,
        output_summary: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        artifact_refs: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> Optional[TraceSpan]:
        resolved_span_id = span_id or get_trace_context()["span_id"]
        if not resolved_span_id:
            logger.warning("TraceService write skipped: missing span_id for span completion")
            return None
        try:
            span = self.db.query(TraceSpan).filter(TraceSpan.span_id == resolved_span_id).first()
            if span is None:
                logger.warning("TraceService write skipped: trace span %s not found", resolved_span_id)
                return None
            ended_at = _utcnow_naive()
            span.status = status
            span.ended_at = ended_at
            span.duration_ms = _duration_ms(span.started_at, ended_at)
            if output_summary is not None:
                span.output_summary = output_summary
            if metrics is not None:
                span.metrics = metrics
            if artifact_refs is not None:
                span.artifact_refs = artifact_refs
            if error_message is not None:
                span.error_message = error_message
            self.db.commit()
            self.db.refresh(span)
            return span
        except Exception as exc:
            self._rollback_warning(exc)
            return None

    def _commit_new(self, obj: Any) -> Any:
        try:
            self.db.add(obj)
            self.db.commit()
            self.db.refresh(obj)
            return obj
        except Exception as exc:
            self._rollback_warning(exc)
            return None

    def _rollback_warning(self, exc: Exception) -> None:
        try:
            self.db.rollback()
        except Exception:
            pass
        logger.warning("TraceService write failed: %s", exc, exc_info=True)


def _duration_ms(started_at: Optional[datetime], ended_at: datetime) -> Optional[int]:
    if started_at is None:
        return None
    return max(0, int((ended_at - started_at).total_seconds() * 1000))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
