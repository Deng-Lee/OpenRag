"""Destructive Milvus operations isolated behind explicit generation guards."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pymilvus import Collection, connections, utility


class CleanupGuardError(RuntimeError):
    pass


class PyMilvusCleanupBackend:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str | None = None,
        password: str | None = None,
    ):
        self.alias = "openrag_cleanup"
        kwargs = {"alias": self.alias, "host": host, "port": port}
        if user:
            kwargs.update(user=user, password=password or "")
        connections.connect(**kwargs)

    def aliases_for(self, name):
        return (
            tuple(utility.list_aliases(name, using=self.alias))
            if utility.has_collection(name, using=self.alias)
            else ()
        )

    def drop_collection(self, name):
        if utility.has_collection(name, using=self.alias):
            Collection(name, using=self.alias).drop()

    def release_collection(self, name):
        if utility.has_collection(name, using=self.alias):
            Collection(name, using=self.alias).release()

    def has_collection(self, name):
        return utility.has_collection(name, using=self.alias)


@dataclass(frozen=True)
class DeletePlan:
    generation_id: str
    collections: tuple[str, ...]


def _read(value, field):
    return value.get(field) if isinstance(value, dict) else getattr(value, field)


class MilvusCollectionCleaner:
    def __init__(self, backend):
        self.backend = backend

    def build_delete_plan(self, generation):
        names = tuple(
            name
            for name in (
                _read(generation, "chunk_collection_name"),
                _read(generation, "layer_collection_name"),
            )
            if name
        )
        return DeletePlan(str(_read(generation, "id")), names)

    def assert_deletable(self, generation, *, active_id, previous_id):
        generation_id = str(_read(generation, "id"))
        if _read(generation, "state") not in {"retired", "deleting"}:
            raise CleanupGuardError("Only retired/deleting generations can be deleted")
        if generation_id in {active_id, previous_id}:
            raise CleanupGuardError("Route-referenced generation cannot be deleted")
        delete_after = _read(generation, "delete_after")
        if delete_after is not None and delete_after.tzinfo is None:
            delete_after = delete_after.replace(tzinfo=timezone.utc)
        if delete_after is None or delete_after > datetime.now(timezone.utc):
            raise CleanupGuardError("Generation retention period has not expired")
        if any(
            self.backend.aliases_for(name)
            for name in self.build_delete_plan(generation).collections
        ):
            raise CleanupGuardError("Alias-targeted Collection cannot be deleted")

    def drop_generation_collections(
        self, generation, *, active_id, previous_id, confirmation
    ):
        plan = self.build_delete_plan(generation)
        if confirmation != plan.generation_id:
            raise CleanupGuardError("Exact generation id confirmation is required")
        self.assert_deletable(generation, active_id=active_id, previous_id=previous_id)
        for name in plan.collections:
            self.backend.drop_collection(name)
        return plan

    def verify_collections_absent(self, generation) -> bool:
        return all(
            not self.backend.has_collection(name)
            for name in self.build_delete_plan(generation).collections
        )
