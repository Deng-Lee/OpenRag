"""Explicit, read-only inspection and PostgreSQL registration of legacy Milvus data."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from pymilvus import Collection, connections, utility
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openrag.config import EmbeddingConfig
from openrag.indexing.fingerprint import (
    build_embedding_manifest,
    compute_embedding_fingerprint,
)
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationRoute,
    IndexGenerationState,
)


class LegacyBootstrapError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LegacyCollectionSnapshot:
    name: str
    fields: tuple[dict[str, Any], ...]
    indexes: tuple[dict[str, Any], ...]
    entity_count: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class LegacyBootstrapResult:
    generation_id: str
    created: bool
    dry_run: bool
    manifest: dict[str, Any]


class CollectionInspector(Protocol):
    def inspect_collection(self, name: str) -> LegacyCollectionSnapshot: ...


class MilvusLegacyInspector:
    """A read-only Milvus metadata client; it never creates, aliases or drops resources."""

    def __init__(self, *, host: str, port: int):
        self.alias = f"legacy_bootstrap_{uuid4().hex}"
        connections.connect(alias=self.alias, host=host, port=port)

    def close(self) -> None:
        connections.disconnect(self.alias)

    def inspect_collection(self, name: str) -> LegacyCollectionSnapshot:
        if not utility.has_collection(name, using=self.alias):
            raise LegacyBootstrapError(
                "LEGACY_COLLECTION_MISSING",
                f"Legacy Collection {name!r} does not exist",
            )
        collection = Collection(name, using=self.alias)
        fields = []
        for field in collection.schema.fields:
            item: dict[str, Any] = {
                "name": field.name,
                "type": getattr(field.dtype, "name", str(field.dtype)),
                "is_primary": bool(getattr(field, "is_primary", False)),
            }
            params = dict(getattr(field, "params", {}) or {})
            if "dim" in params:
                item["dim"] = int(params["dim"])
            if "max_length" in params:
                item["max_length"] = int(params["max_length"])
            fields.append(item)
        indexes = []
        for index in collection.indexes:
            params = dict(getattr(index, "params", {}) or {})
            indexes.append(
                {
                    "field_name": getattr(index, "field_name", None),
                    "index_type": params.get("index_type"),
                    "metric_type": params.get("metric_type"),
                    "params": params.get("params", {}),
                }
            )
        aliases = tuple(sorted(utility.list_aliases(name, using=self.alias)))
        return LegacyCollectionSnapshot(
            name=name,
            fields=tuple(fields),
            indexes=tuple(indexes),
            entity_count=int(collection.num_entities),
            aliases=aliases,
        )


_REQUIRED_CHUNK_FIELDS = {
    "chunk_id",
    "file_id",
    "text",
    "embedding",
    "page",
    "level",
    "block_type",
}
_REQUIRED_LAYER_FIELDS = {"layer_row_id", "file_id", "layer", "text", "embedding"}


def _validate_snapshot(
    snapshot: LegacyCollectionSnapshot,
    *,
    required_fields: set[str],
    expected_dimension: int,
) -> None:
    fields = {field.get("name"): field for field in snapshot.fields}
    missing = sorted(required_fields - set(fields))
    vector_field = fields.get("embedding", {})
    metric_types = {
        str(index.get("metric_type") or "").upper()
        for index in snapshot.indexes
        if index.get("field_name") == "embedding"
    }
    if (
        missing
        or vector_field.get("type") != "FLOAT_VECTOR"
        or int(vector_field.get("dim") or 0) != expected_dimension
        or "COSINE" not in metric_types
    ):
        raise LegacyBootstrapError(
            "LEGACY_SCHEMA_MISMATCH",
            f"Legacy Collection {snapshot.name!r} does not match the configured schema",
        )


def inspect_legacy_collections(
    inspector: CollectionInspector,
    *,
    expected_dimension: int,
    chunk_collection_name: str = "openrag_chunks",
    layer_collection_name: str = "openrag_layers",
) -> tuple[LegacyCollectionSnapshot, LegacyCollectionSnapshot]:
    chunk = inspector.inspect_collection(chunk_collection_name)
    layer = inspector.inspect_collection(layer_collection_name)
    _validate_snapshot(
        chunk,
        required_fields=_REQUIRED_CHUNK_FIELDS,
        expected_dimension=expected_dimension,
    )
    _validate_snapshot(
        layer,
        required_fields=_REQUIRED_LAYER_FIELDS,
        expected_dimension=expected_dimension,
    )
    return chunk, layer


def build_legacy_manifest(
    *,
    embedding_config: EmbeddingConfig,
    chunk: LegacyCollectionSnapshot,
    layer: LegacyCollectionSnapshot,
    operator: str,
    bootstrap_at: datetime,
) -> dict[str, Any]:
    embedding_manifest = build_embedding_manifest(embedding_config)
    verified_identity = bool(
        embedding_config.revision.strip() and embedding_config.model_identity.strip()
    )
    return {
        "legacy": True,
        "identity_confidence": "verified" if verified_identity else "declared",
        "embedding": embedding_manifest,
        "embedding_fingerprint": compute_embedding_fingerprint(embedding_manifest),
        "observed_chunk_schema": list(chunk.fields),
        "observed_layer_schema": list(layer.fields),
        "observed_chunk_indexes": list(chunk.indexes),
        "observed_layer_indexes": list(layer.indexes),
        "observed_chunk_count": chunk.entity_count,
        "observed_layer_count": layer.entity_count,
        "observed_chunk_aliases": list(chunk.aliases),
        "observed_layer_aliases": list(layer.aliases),
        "bootstrap_at": bootstrap_at.isoformat(),
        "bootstrap_operator": operator,
    }


def bootstrap_legacy_generation(
    db: Session,
    *,
    inspector: CollectionInspector,
    embedding_config: EmbeddingConfig,
    operator: str,
    operator_id: int | None = None,
    scope: str = "global",
    chunk_collection_name: str = "openrag_chunks",
    layer_collection_name: str = "openrag_layers",
    dry_run: bool = False,
) -> LegacyBootstrapResult:
    now = datetime.now(timezone.utc)
    chunk, layer = inspect_legacy_collections(
        inspector,
        expected_dimension=embedding_config.dimension,
        chunk_collection_name=chunk_collection_name,
        layer_collection_name=layer_collection_name,
    )
    manifest = build_legacy_manifest(
        embedding_config=embedding_config,
        chunk=chunk,
        layer=layer,
        operator=operator,
        bootstrap_at=now,
    )
    if dry_run:
        return LegacyBootstrapResult(
            generation_id="dry-run",
            created=False,
            dry_run=True,
            manifest=manifest,
        )

    existing = _get_registered_legacy(
        db,
        scope=scope,
        manifest=manifest,
        chunk_collection_name=chunk_collection_name,
        layer_collection_name=layer_collection_name,
    )
    if existing is not None:
        return LegacyBootstrapResult(
            generation_id=existing.id,
            created=False,
            dry_run=False,
            manifest=existing.manifest,
        )

    generation_id = str(uuid4())
    generation = IndexGeneration(
        id=generation_id,
        scope=scope,
        state=IndexGenerationState.ACTIVE.value,
        embedding_provider=embedding_config.provider.strip().lower(),
        embedding_model=embedding_config.model.strip(),
        embedding_revision=embedding_config.revision.strip() or "declared",
        embedding_dimension=embedding_config.dimension,
        embedding_fingerprint=manifest["embedding_fingerprint"],
        embedding_config_ref=embedding_config.config_ref.strip() or "legacy/global",
        vector_normalization=embedding_config.normalization.strip().lower(),
        distance_metric=embedding_config.distance_metric.strip().upper(),
        schema_version=1,
        chunk_policy_revision="legacy-declared",
        hierarchy_policy_revision="legacy-declared",
        chunk_collection_name=chunk.name,
        layer_collection_name=layer.name,
        manifest=manifest,
        expected_chunk_count=chunk.entity_count,
        expected_layer_count=layer.entity_count,
        indexed_chunk_count=chunk.entity_count,
        indexed_layer_count=layer.entity_count,
        created_by=operator_id,
        activated_at=now,
    )
    route = IndexGenerationRoute(
        scope=scope,
        active_generation_id=generation_id,
        route_version=1,
        activated_at=now,
        activated_by=operator_id,
        write_barrier=False,
        updated_at=now,
    )
    db.add_all([generation, route])
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _get_registered_legacy(
            db,
            scope=scope,
            manifest=manifest,
            chunk_collection_name=chunk_collection_name,
            layer_collection_name=layer_collection_name,
        )
        if existing is None:
            raise
        return LegacyBootstrapResult(
            generation_id=existing.id,
            created=False,
            dry_run=False,
            manifest=existing.manifest,
        )
    return LegacyBootstrapResult(
        generation_id=generation_id,
        created=True,
        dry_run=False,
        manifest=manifest,
    )


def _get_registered_legacy(
    db: Session,
    *,
    scope: str,
    manifest: dict[str, Any],
    chunk_collection_name: str,
    layer_collection_name: str,
) -> IndexGeneration | None:
    route = db.get(IndexGenerationRoute, scope)
    if route is None:
        return None
    generation = db.get(IndexGeneration, route.active_generation_id)
    if generation is None:
        raise LegacyBootstrapError(
            "LEGACY_ROUTE_INVALID",
            "Active legacy route references a missing generation",
        )
    if not generation.manifest.get("legacy"):
        raise LegacyBootstrapError(
            "LEGACY_ALREADY_VERSIONED",
            "Active route already points to a versioned generation",
        )
    if (
        generation.chunk_collection_name != chunk_collection_name
        or generation.layer_collection_name != layer_collection_name
        or generation.embedding_fingerprint != manifest["embedding_fingerprint"]
    ):
        raise LegacyBootstrapError(
            "LEGACY_REGISTRATION_CONFLICT",
            "Registered legacy generation does not match current Collection or embedding identity",
        )
    return generation


def verify_legacy_route(db: Session, scope: str = "global") -> IndexGeneration:
    route = db.get(IndexGenerationRoute, scope)
    if route is None:
        raise LegacyBootstrapError(
            "ACTIVE_ROUTE_MISSING", "Active index route is not registered"
        )
    generation = db.get(IndexGeneration, route.active_generation_id)
    if generation is None or generation.state != IndexGenerationState.ACTIVE.value:
        raise LegacyBootstrapError(
            "ACTIVE_ROUTE_INVALID", "Active index generation is unavailable"
        )
    if not generation.manifest.get("legacy"):
        raise LegacyBootstrapError(
            "ACTIVE_ROUTE_NOT_LEGACY", "Active generation is not legacy"
        )
    return generation


def validate_active_route_on_startup(
    db: Session, scope: str = "global"
) -> dict[str, Any]:
    route = db.get(IndexGenerationRoute, scope)
    if route is None:
        raise LegacyBootstrapError(
            "ACTIVE_ROUTE_MISSING", "Active index route is not registered"
        )
    generation = db.get(IndexGeneration, route.active_generation_id)
    if generation is None or generation.state != IndexGenerationState.ACTIVE.value:
        raise LegacyBootstrapError(
            "ACTIVE_ROUTE_INVALID", "Active index generation is unavailable"
        )
    return {
        "scope": scope,
        "active_generation_id": generation.id,
        "route_version": route.route_version,
        "embedding_fingerprint": generation.embedding_fingerprint,
        "chunk_collection_name": generation.chunk_collection_name,
        "layer_collection_name": generation.layer_collection_name,
        "legacy": bool(generation.manifest.get("legacy")),
    }


def print_legacy_bootstrap_dry_run(db: Session, **kwargs: Any) -> str:
    result = bootstrap_legacy_generation(db, dry_run=True, **kwargs)
    return json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True)
