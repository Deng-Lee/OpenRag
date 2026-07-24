"""Fail-closed retirement and explicit deletion planning."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from openrag.indexing.backup_service import BackupRequiredError, BackupService
from openrag.indexing.milvus_cleaner import CleanupGuardError, MilvusCollectionCleaner
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute, IndexGenerationState
from openrag.models.task import Task, TaskStatus


class RetentionService:
    def __init__(self, db: Session, cleaner: MilvusCollectionCleaner, backup: BackupService):
        self.db = db
        self.cleaner = cleaner
        self.backup = backup

    @staticmethod
    def calculate_delete_after(retired_at: datetime, retention_seconds: int) -> datetime:
        return retired_at + timedelta(seconds=retention_seconds)

    def list_retention_candidates(self, now=None):
        now = now or datetime.now(timezone.utc)
        return (
            self.db.query(IndexGeneration)
            .filter(
                IndexGeneration.state == IndexGenerationState.RETIRED.value,
                IndexGeneration.delete_after.is_not(None),
                IndexGeneration.delete_after <= now,
            )
            .all()
        )

    def release_retired_generation(self, generation_id: str) -> None:
        generation = self.db.get(IndexGeneration, generation_id)
        self.assert_deletable(generation, require_retention=False)
        for name in self.cleaner.build_delete_plan(generation).collections:
            self.cleaner.backend.release_collection(name)

    def build_delete_plan(self, generation_id: str) -> dict:
        generation = self.db.get(IndexGeneration, generation_id)
        if generation is None:
            raise CleanupGuardError("Generation does not exist")
        route = self.db.get(IndexGenerationRoute, generation.scope)
        aliases = {
            name: list(self.cleaner.backend.aliases_for(name))
            for name in self.cleaner.build_delete_plan(generation).collections
        }
        return {
            "generation_id": generation.id,
            "state": generation.state,
            "collections": list(self.cleaner.build_delete_plan(generation).collections),
            "active_generation_id": route.active_generation_id if route else None,
            "previous_generation_id": route.previous_generation_id if route else None,
            "delete_after": generation.delete_after.isoformat() if generation.delete_after else None,
            "aliases": aliases,
            "backup_required": self.backup.should_backup_before_delete(generation),
        }

    def assert_deletable(self, generation, *, require_retention=True) -> None:
        if generation is None:
            raise CleanupGuardError("Generation does not exist")
        route = self.db.get(IndexGenerationRoute, generation.scope)
        active_id = route.active_generation_id if route else None
        previous_id = route.previous_generation_id if route else None
        if require_retention:
            self.cleaner.assert_deletable(generation, active_id=active_id, previous_id=previous_id)
        elif generation.state != IndexGenerationState.RETIRED.value or generation.id in {active_id, previous_id}:
            raise CleanupGuardError("Only unreferenced retired generations can be released")
        if any(
            self.cleaner.backend.aliases_for(name)
            for name in self.cleaner.build_delete_plan(generation).collections
        ):
            raise CleanupGuardError("Alias-targeted generation cannot be released")
        running = (
            self.db.query(Task.id)
            .filter(
                Task.index_generation_id == generation.id,
                Task.status.in_([TaskStatus.PENDING.value, TaskStatus.RETRY.value, TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value]),
            )
            .first()
        )
        if running:
            raise CleanupGuardError("Generation still has writable tasks")
        if require_retention and self.backup.should_backup_before_delete(generation) and not self.backup.verify_backup_manifest(generation):
            raise BackupRequiredError("Required generation backup is not complete")

    def mark_deleting(self, generation_id: str, *, deleted_by: int, plan: dict) -> None:
        generation = self.db.get(IndexGeneration, generation_id)
        self.assert_deletable(generation)
        generation.state = IndexGenerationState.DELETING.value
        generation.deletion_plan = plan
        generation.deleted_by = deleted_by
        self.db.commit()

    def mark_deleted(self, generation_id: str) -> None:
        generation = self.db.get(IndexGeneration, generation_id)
        if generation.state != IndexGenerationState.DELETING.value:
            raise CleanupGuardError("Generation is not deleting")
        if not self.cleaner.verify_collections_absent(generation):
            raise CleanupGuardError("Generation collections still exist")
        generation.state = IndexGenerationState.DELETED.value
        generation.deleted_at = datetime.now(timezone.utc)
        self.db.commit()
