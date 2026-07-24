"""Recoverable scheduling and progress for file-level candidate rebuilds."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from openrag.indexing.source_reader import GenerationSourceReader
from openrag.models.index_generation import (
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationState,
)
from openrag.models.task import Task, TaskStatus, TaskType
from openrag.services.index_generation_service import (
    GenerationStateTransitionError,
    IndexGenerationService,
)
from openrag.services.task_service import TaskService


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GenerationReindexService:
    def __init__(
        self, db: Session, source_reader: GenerationSourceReader | None = None
    ):
        self.db = db
        self.source_reader = source_reader
        self.generation_service = IndexGenerationService(db)

    def initialize_generation_files(self, generation_id: str) -> int:
        if self.source_reader is None:
            raise RuntimeError("GENERATION_SOURCE_READER_REQUIRED")
        generation = self.generation_service._require_generation(generation_id)
        if generation.state != IndexGenerationState.BUILDING.value:
            raise GenerationStateTransitionError(
                "Generation files can only be initialized while building"
            )
        if generation.source_watermark_at is None:
            generation.source_watermark_at = _now()
        initialized = 0
        for file in self.source_reader.list_active_source_files():
            source = self.source_reader.get_source_revision(file.id)
            row = self.db.get(IndexGenerationFile, (generation_id, file.id))
            if row is None:
                row = IndexGenerationFile(
                    generation_id=generation_id,
                    file_id=file.id,
                    workspace_id=file.workspace_id,
                    state=IndexGenerationFileState.PENDING.value,
                    source_content_hash=source.source_content_hash,
                    source_updated_at=source.source_updated_at,
                    expected_chunk_count=len(source.chunks),
                    expected_layer_count=len(source.layers),
                )
                self.db.add(row)
                initialized += 1
            elif row.source_content_hash != source.source_content_hash:
                row.state = IndexGenerationFileState.STALE.value
                row.source_content_hash = source.source_content_hash
                row.source_updated_at = source.source_updated_at
                row.expected_chunk_count = len(source.chunks)
                row.expected_layer_count = len(source.layers)
                row.written_chunk_count = 0
                row.written_layer_count = 0
                row.error_code = None
                row.error = None
                row.completed_at = None
                initialized += 1
        self.db.commit()
        self.recalculate_generation_progress(generation_id)
        return initialized

    def enqueue_pending_files(
        self,
        generation_id: str,
        *,
        user_id: int,
        limit: int = 100,
        max_concurrent: int = 4,
    ) -> list[Task]:
        generation = self.generation_service._require_generation(generation_id)
        if generation.build_paused:
            return []
        active_count = (
            self.db.query(Task)
            .filter(
                Task.index_generation_id == generation_id,
                Task.task_type.in_(
                    [
                        TaskType.REINDEX_GENERATION_FILE.value,
                        TaskType.RECONCILE_GENERATION_FILE.value,
                    ]
                ),
                Task.status.in_([TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value]),
            )
            .count()
        )
        available = max(0, min(limit, max_concurrent - active_count))
        if available == 0:
            return []
        rows = (
            self.db.query(IndexGenerationFile)
            .filter(
                IndexGenerationFile.generation_id == generation_id,
                or_(
                    IndexGenerationFile.state.in_(
                        [
                            IndexGenerationFileState.PENDING.value,
                            IndexGenerationFileState.STALE.value,
                        ]
                    ),
                    and_(
                        IndexGenerationFile.state
                        == IndexGenerationFileState.RETRY.value,
                        IndexGenerationFile.next_retry_at <= _now(),
                    ),
                ),
            )
            .order_by(IndexGenerationFile.file_id)
            .limit(available)
            .all()
        )
        tasks = []
        task_service = TaskService(self.db)
        for row in rows:
            exists = (
                self.db.query(Task.id)
                .filter(
                    Task.index_generation_id == generation_id,
                    Task.file_id == row.file_id,
                    Task.task_type.in_(
                        [
                            TaskType.REINDEX_GENERATION_FILE.value,
                            TaskType.RECONCILE_GENERATION_FILE.value,
                        ]
                    ),
                    Task.status.in_(
                        [
                            TaskStatus.PENDING.value,
                            TaskStatus.RETRY.value,
                            TaskStatus.ASSIGNED.value,
                            TaskStatus.STARTED.value,
                        ]
                    ),
                )
                .first()
            )
            if exists:
                continue
            tasks.append(
                task_service.add_task(
                    workspace_id=row.workspace_id,
                    user_id=user_id,
                    file_id=row.file_id,
                    task_type=TaskType.REINDEX_GENERATION_FILE.value,
                    queue="reindex",
                    priority=1,
                    index_generation_id=generation_id,
                    payload={
                        "generation_id": generation_id,
                        "source_content_hash": row.source_content_hash,
                        "source_updated_at": row.source_updated_at.isoformat(),
                    },
                )
            )
        self.db.commit()
        for task in tasks:
            self.db.refresh(task)
        return tasks

    def claim_file(
        self, generation_id: str, file_id: int, *, worker_id: str
    ) -> IndexGenerationFile | None:
        row = (
            self.db.query(IndexGenerationFile)
            .filter(
                IndexGenerationFile.generation_id == generation_id,
                IndexGenerationFile.file_id == file_id,
                IndexGenerationFile.state.in_(
                    [
                        IndexGenerationFileState.PENDING.value,
                        IndexGenerationFileState.RETRY.value,
                        IndexGenerationFileState.STALE.value,
                    ]
                ),
            )
            .with_for_update(skip_locked=True)
            .one_or_none()
        )
        if row is None:
            return None
        row.state = IndexGenerationFileState.RUNNING.value
        row.worker_id = worker_id
        row.heartbeat_at = _now()
        row.error_code = None
        row.error = None
        self.db.commit()
        self.db.refresh(row)
        return row

    def mark_file_success(self, generation_id: str, file_id: int, result) -> None:
        row = self._require_file(generation_id, file_id)
        row.state = IndexGenerationFileState.SUCCESS.value
        row.source_content_hash = result.source_content_hash
        row.expected_chunk_count = result.expected_chunk_count
        row.written_chunk_count = result.written_chunk_count
        row.expected_layer_count = result.expected_layer_count
        row.written_layer_count = result.written_layer_count
        row.worker_id = None
        row.heartbeat_at = None
        row.next_retry_at = None
        row.error_code = None
        row.error = None
        row.completed_at = _now()
        self.db.commit()
        self.recalculate_generation_progress(generation_id)

    def mark_file_retry(
        self,
        generation_id: str,
        file_id: int,
        *,
        error_code: str,
        error: str,
        delay_seconds: int,
    ) -> None:
        row = self._require_file(generation_id, file_id)
        row.state = IndexGenerationFileState.RETRY.value
        row.retry_count += 1
        row.next_retry_at = _now() + timedelta(seconds=delay_seconds)
        row.worker_id = None
        row.heartbeat_at = None
        row.error_code = error_code
        row.error = error[:500]
        self.db.commit()
        self.recalculate_generation_progress(generation_id)

    def mark_file_failed(
        self,
        generation_id: str,
        file_id: int,
        *,
        error_code: str,
        error: str,
    ) -> None:
        row = self._require_file(generation_id, file_id)
        row.state = IndexGenerationFileState.FAILED.value
        row.worker_id = None
        row.heartbeat_at = None
        row.error_code = error_code
        row.error = error[:500]
        row.completed_at = _now()
        self.db.commit()
        self.recalculate_generation_progress(generation_id)

    def recover_stale_files(
        self, generation_id: str, *, stale_after_seconds: int = 600
    ) -> int:
        threshold = _now() - timedelta(seconds=stale_after_seconds)
        rows = (
            self.db.query(IndexGenerationFile)
            .filter(
                IndexGenerationFile.generation_id == generation_id,
                IndexGenerationFile.state == IndexGenerationFileState.RUNNING.value,
                IndexGenerationFile.heartbeat_at < threshold,
            )
            .all()
        )
        for row in rows:
            row.state = IndexGenerationFileState.STALE.value
            row.worker_id = None
            row.heartbeat_at = None
        self.db.commit()
        return len(rows)

    def recalculate_generation_progress(self, generation_id: str):
        return self.generation_service.recalculate_counts(generation_id)

    def pause_generation(self, generation_id: str) -> None:
        generation = self.generation_service._require_generation(generation_id)
        if generation.state not in {
            IndexGenerationState.BUILDING.value,
            IndexGenerationState.RECONCILING.value,
        }:
            raise GenerationStateTransitionError(
                "Only a building generation can be paused"
            )
        generation.build_paused = True
        self._cancel_unclaimed_tasks(generation_id)
        self.db.commit()

    def resume_generation(self, generation_id: str) -> None:
        generation = self.generation_service._require_generation(generation_id)
        if generation.state not in {
            IndexGenerationState.BUILDING.value,
            IndexGenerationState.RECONCILING.value,
        }:
            raise GenerationStateTransitionError(
                "Only a building generation can be resumed"
            )
        generation.build_paused = False
        self.db.commit()

    def cancel_generation(self, generation_id: str) -> None:
        generation = self.generation_service._require_generation(generation_id)
        active_tasks = (
            self.db.query(Task.id)
            .filter(
                Task.index_generation_id == generation_id,
                Task.task_type.in_(
                    [
                        TaskType.REINDEX_GENERATION_FILE.value,
                        TaskType.RECONCILE_GENERATION_FILE.value,
                    ]
                ),
                Task.status.in_([TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value]),
            )
            .first()
        )
        running_file = (
            self.db.query(IndexGenerationFile.file_id)
            .filter(
                IndexGenerationFile.generation_id == generation_id,
                IndexGenerationFile.state == IndexGenerationFileState.RUNNING.value,
            )
            .first()
        )
        if active_tasks or running_file:
            raise GenerationStateTransitionError(
                "Running rebuild tasks must drain before cancellation"
            )
        generation.build_paused = True
        self._cancel_unclaimed_tasks(generation_id)
        self.db.commit()
        self.generation_service.mark_failed(
            generation_id,
            error_code="GENERATION_BUILD_CANCELLED",
            error="Candidate rebuild cancelled by an administrator",
        )

    def _cancel_unclaimed_tasks(self, generation_id: str) -> int:
        now = _now()
        return (
            self.db.query(Task)
            .filter(
                Task.index_generation_id == generation_id,
                Task.task_type.in_(
                    [
                        TaskType.REINDEX_GENERATION_FILE.value,
                        TaskType.RECONCILE_GENERATION_FILE.value,
                    ]
                ),
                Task.status.in_([TaskStatus.PENDING.value, TaskStatus.RETRY.value]),
            )
            .update(
                {
                    Task.status: TaskStatus.CANCELLED.value,
                    Task.completed_at: now,
                },
                synchronize_session=False,
            )
        )

    def _require_file(self, generation_id: str, file_id: int) -> IndexGenerationFile:
        row = self.db.get(IndexGenerationFile, (generation_id, file_id))
        if row is None:
            raise RuntimeError("INDEX_GENERATION_FILE_NOT_FOUND")
        return row
