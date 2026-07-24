"""Watermark-based candidate reconciliation against canonical file sources."""

from datetime import datetime, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from openrag.indexing.source_reader import GenerationSourceReader
from openrag.models.file import File
from openrag.models.index_generation import (
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationState,
)
from openrag.models.task import Task, TaskStatus, TaskType
from openrag.services.index_generation_service import IndexGenerationService
from openrag.services.task_service import TaskService


class GenerationLagError(RuntimeError):
    code = "INDEX_GENERATION_LAG_NONZERO"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReconciliationService:
    def __init__(self, db: Session, source_reader: GenerationSourceReader):
        self.db = db
        self.source_reader = source_reader
        self.generation_service = IndexGenerationService(db)

    def capture_source_watermark(self, generation_id: str) -> datetime:
        generation = self.generation_service._require_generation(generation_id)
        if generation.source_watermark_at is None:
            generation.source_watermark_at = _now()
            self.db.commit()
        return generation.source_watermark_at

    def scan_changed_files(self, generation_id: str) -> list[IndexGenerationFile]:
        changed = []
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
                changed.append(row)
                continue
            if row.source_content_hash != source.source_content_hash:
                row.state = IndexGenerationFileState.STALE.value
                row.source_content_hash = source.source_content_hash
                row.source_updated_at = source.source_updated_at
                row.expected_chunk_count = len(source.chunks)
                row.expected_layer_count = len(source.layers)
                row.written_chunk_count = 0
                row.written_layer_count = 0
                row.completed_at = None
                row.retry_count = 0
                row.next_retry_at = None
                row.worker_id = None
                row.heartbeat_at = None
                row.error_code = None
                row.error = None
                changed.append(row)
            elif row.source_updated_at != source.source_updated_at:
                row.source_updated_at = source.source_updated_at
        self.db.commit()
        return changed

    def scan_deleted_files(self, generation_id: str) -> list[IndexGenerationFile]:
        rows = (
            self.db.query(IndexGenerationFile)
            .join(File, File.id == IndexGenerationFile.file_id)
            .filter(
                IndexGenerationFile.generation_id == generation_id,
                File.deleted_at.is_not(None),
                IndexGenerationFile.state
                != IndexGenerationFileState.DELETED.value,
            )
            .all()
        )
        for row in rows:
            row.state = IndexGenerationFileState.DELETED.value
            row.completed_at = _now()
            row.worker_id = None
            row.heartbeat_at = None
        self.db.commit()
        return rows

    def enqueue_reconciliation_tasks(
        self, generation_id: str, *, user_id: int, limit: int = 100
    ) -> list[Task]:
        generation = self.generation_service._require_generation(generation_id)
        if generation.build_paused:
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
            .limit(limit)
            .all()
        )
        service = TaskService(self.db)
        tasks = []
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
                service.add_task(
                    workspace_id=row.workspace_id,
                    user_id=user_id,
                    file_id=row.file_id,
                    task_type=TaskType.RECONCILE_GENERATION_FILE.value,
                    queue="reindex",
                    priority=2,
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

    def enqueue_deleted_files(
        self, rows: list[IndexGenerationFile], *, user_id: int
    ) -> list[Task]:
        service = TaskService(self.db)
        tasks = []
        for row in rows:
            existing = (
                self.db.query(Task.id)
                .filter(
                    Task.file_id == row.file_id,
                    Task.task_type == TaskType.PURGE_FILE_FROM_GENERATIONS.value,
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
            if existing:
                continue
            tasks.append(
                service.add_task(
                    workspace_id=row.workspace_id,
                    user_id=user_id,
                    file_id=row.file_id,
                    task_type=TaskType.PURGE_FILE_FROM_GENERATIONS.value,
                    queue="normal",
                    priority=7,
                    payload={"file_id": row.file_id},
                )
            )
        self.db.commit()
        return tasks

    def calculate_build_lag(self, generation_id: str) -> int:
        lag = (
            self.db.query(IndexGenerationFile)
            .filter(
                IndexGenerationFile.generation_id == generation_id,
                IndexGenerationFile.state.notin_(
                    [
                        IndexGenerationFileState.SUCCESS.value,
                        IndexGenerationFileState.DELETED.value,
                    ]
                ),
            )
            .count()
        )
        generation = self.generation_service._require_generation(generation_id)
        generation.build_lag_files = lag
        if lag == 0:
            generation.last_reconciled_at = _now()
        self.db.commit()
        return lag

    def reconcile_until_stable(
        self, generation_id: str, *, user_id: int, max_passes: int = 3
    ) -> int:
        self.capture_source_watermark(generation_id)
        generation = self.generation_service._require_generation(generation_id)
        if generation.state == IndexGenerationState.BUILDING.value:
            self.generation_service.transition_state(
                generation_id, IndexGenerationState.RECONCILING
            )
        lag = 0
        for _ in range(max_passes):
            self.scan_changed_files(generation_id)
            deleted = self.scan_deleted_files(generation_id)
            self.enqueue_deleted_files(deleted, user_id=user_id)
            self.enqueue_reconciliation_tasks(generation_id, user_id=user_id)
            lag = self.calculate_build_lag(generation_id)
            if lag == 0:
                return 0
        return lag

    def assert_zero_lag(self, generation_id: str) -> None:
        lag = self.calculate_build_lag(generation_id)
        if lag != 0:
            raise GenerationLagError(f"Generation has {lag} unreconciled files")
