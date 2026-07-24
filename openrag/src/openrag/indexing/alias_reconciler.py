"""Best-effort Milvus aliases; PostgreSQL route remains authoritative."""

from typing import Any, Callable, Protocol
from uuid import uuid4

from pymilvus import connections, utility


class AliasBackend(Protocol):
    def target(self, alias: str) -> str | None: ...
    def set_alias(self, alias: str, collection: str) -> None: ...


class PyMilvusAliasBackend:
    def __init__(self, *, host: str, port: int, user=None, password=None, secure=False):
        self.alias = f"openrag_alias_admin_{uuid4().hex}"
        kwargs = {
            "alias": self.alias,
            "host": host,
            "port": port,
            "secure": secure,
        }
        if user:
            kwargs.update(user=user, password=password or "")
        connections.connect(**kwargs)

    def target(self, alias: str) -> str | None:
        for collection in utility.list_collections(using=self.alias):
            if alias in utility.list_aliases(collection, using=self.alias):
                return collection
        return None

    def set_alias(self, alias: str, collection: str) -> None:
        current = self.target(alias)
        if current == collection:
            return
        if current is None:
            utility.create_alias(collection, alias, using=self.alias)
        else:
            utility.alter_alias(collection, alias, using=self.alias)


class AliasReconciler:
    CHUNK_ALIAS = "openrag_chunks_active"
    LAYER_ALIAS = "openrag_layers_active"

    def __init__(
        self,
        backend: AliasBackend,
        reporter: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.backend = backend
        self.reporter = reporter

    def get_expected_aliases(self, generation) -> dict[str, str]:
        return {
            self.CHUNK_ALIAS: generation.chunk_collection_name,
            self.LAYER_ALIAS: generation.layer_collection_name,
        }

    def inspect_actual_aliases(self) -> dict[str, str | None]:
        return {
            alias: self.backend.target(alias)
            for alias in (self.CHUNK_ALIAS, self.LAYER_ALIAS)
        }

    def sync_active_aliases(self, generation) -> dict[str, Any]:
        expected = self.get_expected_aliases(generation)
        succeeded = []
        failed = []
        for alias, collection in expected.items():
            try:
                self.backend.set_alias(alias, collection)
                succeeded.append(alias)
            except Exception:
                failed.append(alias)
        try:
            actual = self.inspect_actual_aliases()
        except Exception:
            actual = {}
            failed.extend(alias for alias in expected if alias not in failed)
        result = {
            "expected": expected,
            "actual": actual,
            "succeeded_aliases": succeeded,
            "failed_aliases": failed,
            "passed": not failed,
        }
        if failed:
            self.report_alias_drift(result)
        return result

    def reconcile_aliases(self, generation) -> dict[str, Any]:
        return self.sync_active_aliases(generation)

    def report_alias_drift(self, result: dict[str, Any]) -> None:
        if self.reporter is not None:
            self.reporter(result)
