"""Immutable request-level routing from PostgreSQL to one index generation."""

from dataclasses import dataclass
from threading import RLock
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from openrag.config import EmbeddingConfig, get_config
from openrag.embedding.embedding_engine import EmbeddingEngine
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationRoute,
    IndexGenerationState,
)
from openrag.vectorstore.milvus_layer_store import MilvusLayerStore
from openrag.vectorstore.milvus_store import MilvusStore


class IndexRouteUnavailableError(RuntimeError):
    code = "INDEX_ROUTE_UNAVAILABLE"


@dataclass(frozen=True)
class IndexRuntimeSnapshot:
    generation_id: str
    state: str
    scope: str
    route_version: int
    embedding_fingerprint: str
    embedding_config_ref: str
    embedding_manifest: dict
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int
    vector_normalization: str
    distance_metric: str
    schema_version: int
    chunk_collection_name: str
    layer_collection_name: str | None
    hierarchy_enabled: bool
    es_generation: str | None
    legacy: bool


@dataclass(frozen=True)
class IndexRuntime:
    snapshot: IndexRuntimeSnapshot
    embedding_engine: EmbeddingEngine
    vector_store: MilvusStore
    layer_store: MilvusLayerStore | None


def _snapshot(
    generation: IndexGeneration,
    *,
    route_version: int,
) -> IndexRuntimeSnapshot:
    return IndexRuntimeSnapshot(
        generation_id=generation.id,
        state=generation.state,
        scope=generation.scope,
        route_version=route_version,
        embedding_fingerprint=generation.embedding_fingerprint,
        embedding_config_ref=generation.embedding_config_ref,
        embedding_manifest=dict((generation.manifest or {}).get("embedding") or {}),
        embedding_model=generation.embedding_model,
        embedding_revision=generation.embedding_revision,
        embedding_dimension=generation.embedding_dimension,
        vector_normalization=generation.vector_normalization,
        distance_metric=generation.distance_metric,
        schema_version=generation.schema_version,
        chunk_collection_name=generation.chunk_collection_name,
        layer_collection_name=generation.layer_collection_name,
        hierarchy_enabled=bool(generation.layer_collection_name),
        es_generation=generation.es_generation,
        legacy=bool((generation.manifest or {}).get("legacy")),
    )


class IndexRuntimeResolver:
    def __init__(
        self,
        *,
        runtime_builder: Callable[[IndexRuntimeSnapshot], IndexRuntime] | None = None,
        embedding_config_loader: Callable[[str], EmbeddingConfig] | None = None,
    ):
        self._embedding_config_loader = embedding_config_loader or (
            lambda _config_ref: get_config().embedding
        )
        self._runtime_builder = runtime_builder or self._build_runtime
        self._runtimes: dict[tuple[str, str], IndexRuntime] = {}
        self._lock = RLock()

    def get_active_snapshot(
        self, db: Session, scope: str = "global"
    ) -> IndexRuntimeSnapshot:
        try:
            row = db.execute(
                select(IndexGenerationRoute, IndexGeneration)
                .join(
                    IndexGeneration,
                    IndexGeneration.id == IndexGenerationRoute.active_generation_id,
                )
                .where(IndexGenerationRoute.scope == scope)
            ).one_or_none()
        except SQLAlchemyError as exc:
            raise IndexRouteUnavailableError(
                "Active index route cannot be resolved"
            ) from exc
        if row is None:
            raise IndexRouteUnavailableError("Active index route is not registered")
        route, generation = row
        if generation.state != IndexGenerationState.ACTIVE.value:
            raise IndexRouteUnavailableError("Active index generation is not ready")
        return _snapshot(generation, route_version=route.route_version)

    def get_generation_snapshot(
        self, db: Session, generation_id: str
    ) -> IndexRuntimeSnapshot:
        generation = db.get(IndexGeneration, generation_id)
        if generation is None or generation.state == IndexGenerationState.DELETED.value:
            raise IndexRouteUnavailableError("Index generation is unavailable")
        route = db.get(IndexGenerationRoute, generation.scope)
        return _snapshot(generation, route_version=route.route_version if route else 0)

    def get_runtime(self, snapshot: IndexRuntimeSnapshot) -> IndexRuntime:
        key = (snapshot.generation_id, snapshot.embedding_fingerprint)
        with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                runtime = self._runtime_builder(snapshot)
                self._runtimes[key] = runtime
            return runtime

    def invalidate_route(self) -> None:
        """Route snapshots are not cached; retained for explicit caller semantics."""

    def evict_retired_runtime(self, generation_id: str) -> None:
        with self._lock:
            for key in [key for key in self._runtimes if key[0] == generation_id]:
                self._runtimes.pop(key, None)

    def probe_active_runtime(self, db: Session, scope: str = "global") -> IndexRuntime:
        runtime = self.get_runtime(self.get_active_snapshot(db, scope))
        runtime.embedding_engine.probe()
        runtime.vector_store.probe()
        if runtime.layer_store is not None:
            runtime.layer_store.probe()
        return runtime

    def _build_runtime(self, snapshot: IndexRuntimeSnapshot) -> IndexRuntime:
        engine = build_embedding_engine_from_snapshot(
            snapshot, self._embedding_config_loader
        )
        chunk_store, layer_store = build_vector_stores_from_snapshot(snapshot)
        return IndexRuntime(
            snapshot=snapshot,
            embedding_engine=engine,
            vector_store=chunk_store,
            layer_store=layer_store,
        )


def build_embedding_engine_from_snapshot(
    snapshot: IndexRuntimeSnapshot,
    config_loader: Callable[[str], EmbeddingConfig] | None = None,
) -> EmbeddingEngine:
    loader = config_loader or _load_embedding_config
    base_config = loader(snapshot.embedding_config_ref)
    identity = snapshot.embedding_manifest
    updates = {
        "provider": identity.get("provider", base_config.provider),
        "model": identity.get("model", snapshot.embedding_model),
        "revision": identity.get("model_revision", snapshot.embedding_revision),
        "model_identity": identity.get("model_identity", base_config.model_identity),
        "dimension": identity.get("dimension", snapshot.embedding_dimension),
        "input_type": identity.get("input_type", base_config.input_type),
        "encoding_format": identity.get(
            "encoding_format", base_config.encoding_format
        ),
        "normalization": identity.get(
            "normalization", snapshot.vector_normalization
        ),
        "distance_metric": identity.get("distance_metric", snapshot.distance_metric),
        "query_prefix_revision": identity.get(
            "query_prefix_revision", base_config.query_prefix_revision
        ),
        "document_prefix_revision": identity.get(
            "document_prefix_revision", base_config.document_prefix_revision
        ),
        "text_preprocess_revision": identity.get(
            "text_preprocess_revision", base_config.text_preprocess_revision
        ),
        "sdk_contract_revision": identity.get(
            "sdk_contract_revision", base_config.sdk_contract_revision
        ),
        "config_ref": snapshot.embedding_config_ref,
    }
    engine = EmbeddingEngine(config=base_config.model_copy(update=updates))
    engine.assert_fingerprint(snapshot.embedding_fingerprint)
    return engine


def build_vector_stores_from_snapshot(
    snapshot: IndexRuntimeSnapshot,
) -> tuple[MilvusStore, MilvusLayerStore | None]:
        vector = get_config().vector_db
        password = (
            vector.runtime_password.get_secret_value()
            if vector.runtime_password is not None
            else None
        )
        common = {
            "host": vector.host,
            "port": vector.port,
            "dimension": snapshot.embedding_dimension,
            "expected_schema_version": snapshot.schema_version,
            "expected_embedding_fingerprint": (
                None if snapshot.legacy else snapshot.embedding_fingerprint
            ),
            "user": vector.runtime_user,
            "password": password,
            "secure": vector.secure,
            "connection_timeout_seconds": vector.connection_timeout_seconds,
        }
        chunk_store = MilvusStore(
            collection_name=snapshot.chunk_collection_name,
            **common,
        )
        layer_store = (
            MilvusLayerStore(collection_name=snapshot.layer_collection_name, **common)
            if snapshot.layer_collection_name
            else None
        )
        return chunk_store, layer_store


def build_runtime_from_snapshot(snapshot: IndexRuntimeSnapshot) -> IndexRuntime:
    return IndexRuntimeResolver()._build_runtime(snapshot)
