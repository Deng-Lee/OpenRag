"""Policy gate around an external Milvus backup backend."""

from typing import Any, Protocol


class BackupBackend(Protocol):
    def create_backup(self, generation_id: str, collections: list[str]) -> dict[str, Any]: ...
    def get_backup(self, backup_id: str) -> dict[str, Any]: ...


class BackupRequiredError(RuntimeError):
    code = "GENERATION_BACKUP_REQUIRED"


class BackupService:
    def __init__(self, backend: BackupBackend | None, *, threshold_entities: int = 1_000_000):
        self.backend = backend
        self.threshold_entities = threshold_entities

    def should_backup_before_delete(self, generation) -> bool:
        total = int(generation.indexed_chunk_count) + int(generation.indexed_layer_count)
        return total >= self.threshold_entities

    def create_generation_backup(self, generation) -> dict[str, Any]:
        if self.backend is None:
            raise BackupRequiredError("Backup backend is not configured")
        result = self.backend.create_backup(
            generation.id,
            [
                name
                for name in (
                    generation.chunk_collection_name,
                    generation.layer_collection_name,
                )
                if name
            ],
        )
        generation.backup_id = result.get("backup_id")
        generation.backup_status = result.get("status")
        return result

    def verify_backup_manifest(self, generation) -> bool:
        if not generation.backup_id or self.backend is None:
            return False
        manifest = self.backend.get_backup(generation.backup_id)
        return (
            manifest.get("status") == "completed"
            and manifest.get("generation_id") == generation.id
            and set(manifest.get("collections", []))
            == {
                generation.chunk_collection_name,
                generation.layer_collection_name,
            }
        )
