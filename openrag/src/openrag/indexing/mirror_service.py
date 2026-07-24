"""Keep route.previous queryable at a measured rollback RPO."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from openrag.indexing.source_reader import GenerationSourceReader
from openrag.models.file import File
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationRoute,
)
from openrag.models.task import Task, TaskStatus, TaskType
from openrag.services.task_service import TaskService


def _now():
    return datetime.now(timezone.utc)


class PreviousGenerationMirrorService:
    def __init__(self, db: Session, source_reader: GenerationSourceReader):
        self.db = db
        self.source_reader = source_reader

    def _route_previous(self):
        route = self.db.get(IndexGenerationRoute, "global")
        if route is None or route.previous_generation_id is None:
            return route, None
        return route, self.db.get(IndexGeneration, route.previous_generation_id)

    def enqueue_file_mirror(
        self, file_id: int, *, user_id: int, _recalculate: bool = True
    ) -> Task | None:
        route, previous = self._route_previous()
        if previous is None or self.stop_mirroring():
            return None
        source = self.source_reader.get_source_revision(file_id)
        row = self.db.get(IndexGenerationFile, (previous.id, file_id))
        if row is None:
            row = IndexGenerationFile(
                generation_id=previous.id,
                file_id=file_id,
                workspace_id=source.workspace_id,
                state=IndexGenerationFileState.PENDING.value,
                source_content_hash=source.source_content_hash,
                source_updated_at=source.source_updated_at,
                expected_chunk_count=len(source.chunks),
                expected_layer_count=len(source.layers),
            )
            self.db.add(row)
        elif (
            row.state == IndexGenerationFileState.SUCCESS.value
            and row.source_content_hash == source.source_content_hash
        ):
            return None
        else:
            row.state = IndexGenerationFileState.STALE.value
            row.source_content_hash = source.source_content_hash
            row.source_updated_at = source.source_updated_at
            row.expected_chunk_count = len(source.chunks)
            row.expected_layer_count = len(source.layers)
            row.completed_at = None
            row.error_code = None
            row.error = None
        existing = (
            self.db.query(Task)
            .filter(
                Task.index_generation_id == previous.id,
                Task.file_id == file_id,
                Task.task_type == TaskType.MIRROR_PREVIOUS_GENERATION_FILE.value,
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
            self.db.commit()
            return existing
        task = TaskService(self.db).add_task(
            workspace_id=source.workspace_id,
            user_id=user_id,
            file_id=file_id,
            task_type=TaskType.MIRROR_PREVIOUS_GENERATION_FILE.value,
            queue="reindex",
            priority=3,
            index_generation_id=previous.id,
            payload={
                "generation_id": previous.id,
                "source_content_hash": source.source_content_hash,
                "source_updated_at": source.source_updated_at.isoformat(),
            },
        )
        self.db.commit()
        if _recalculate:
            self.calculate_previous_lag()
        return task

    def mirror_file(self, file_id: int, *, user_id: int) -> Task | None:
        return self.enqueue_file_mirror(file_id, user_id=user_id)

    def propagate_delete(self, file_id: int) -> None:
        _, previous = self._route_previous()
        if previous is None:
            return
        row = self.db.get(IndexGenerationFile, (previous.id, file_id))
        if row is not None:
            row.state = IndexGenerationFileState.DELETED.value
            row.completed_at = _now()
            self.db.commit()
        self.calculate_previous_lag()

    def calculate_previous_lag(self) -> int:
        _, previous = self._route_previous()
        if previous is None:
            return 0
        active_files = self.source_reader.list_active_source_files()
        lag = 0
        for file in active_files:
            source = self.source_reader.get_source_revision(file.id)
            row = self.db.get(IndexGenerationFile, (previous.id, file.id))
            if (
                row is None
                or row.state != IndexGenerationFileState.SUCCESS.value
                or row.source_content_hash != source.source_content_hash
            ):
                lag += 1
        lag += (
            self.db.query(IndexGenerationFile)
            .join(File, File.id == IndexGenerationFile.file_id)
            .filter(
                IndexGenerationFile.generation_id == previous.id,
                File.deleted_at.is_not(None),
                IndexGenerationFile.state
                != IndexGenerationFileState.DELETED.value,
            )
            .count()
        )
        previous.mirror_lag_files = lag
        if lag == 0:
            previous.last_mirrored_at = _now()
        self.db.commit()
        return lag

    def reconcile_previous(self, *, user_id: int) -> int:
        if self.stop_mirroring():
            return self.calculate_previous_lag()
        for file in self.source_reader.list_active_source_files():
            self.enqueue_file_mirror(
                file.id, user_id=user_id, _recalculate=False
            )
        return self.calculate_previous_lag()

    def stop_mirroring(self) -> bool:
        route = self.db.get(IndexGenerationRoute, "global")
        if route is None or route.previous_generation_id is None:
            return True
        deadline = route.rollback_deadline
        if deadline is None:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline <= _now()
