"""Credential-free generation health snapshot; route remains authoritative."""

from typing import Any, Callable

from sqlalchemy.orm import Session

from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute, IndexGenerationState


def check_active_generation_readiness(
    generation: IndexGeneration | None,
    runtime_probe: Callable[[str], dict[str, bool]] | None = None,
) -> dict[str, Any]:
    if generation is None or generation.state != IndexGenerationState.ACTIVE.value:
        return {"ready": False, "chunk_collection_ready": False, "layer_collection_ready": False}
    if runtime_probe is None:
        return {"ready": True, "chunk_collection_ready": True, "layer_collection_ready": True}
    try:
        result = runtime_probe(generation.id)
        chunk = bool(result.get("chunks"))
        layer = bool(result.get("layers"))
        return {"ready": chunk and layer, "chunk_collection_ready": chunk, "layer_collection_ready": layer}
    except Exception:
        return {"ready": False, "chunk_collection_ready": False, "layer_collection_ready": False}


def check_alias_consistency(expected: dict[str, str], actual: dict[str, str | None]) -> bool:
    return all(actual.get(alias) == collection for alias, collection in expected.items())


def build_index_health_snapshot(
    db: Session,
    *,
    runtime_probe: Callable[[str], dict[str, bool]] | None = None,
    alias_snapshot: Callable[[], dict[str, str | None]] | None = None,
) -> dict[str, Any]:
    route = db.get(IndexGenerationRoute, "global")
    active = db.get(IndexGeneration, route.active_generation_id) if route else None
    previous = (
        db.get(IndexGeneration, route.previous_generation_id)
        if route and route.previous_generation_id
        else None
    )
    readiness = check_active_generation_readiness(active, runtime_probe)
    expected_aliases = (
        {
            "openrag_chunks_active": active.chunk_collection_name,
            "openrag_layers_active": active.layer_collection_name,
        }
        if active
        else {}
    )
    try:
        alias_consistent = (
            check_alias_consistency(expected_aliases, alias_snapshot())
            if alias_snapshot is not None and expected_aliases
            else None
        )
    except Exception:
        alias_consistent = False
    candidates = (
        db.query(IndexGeneration)
        .filter(
            IndexGeneration.state.in_(
                [
                    IndexGenerationState.BUILDING.value,
                    IndexGenerationState.RECONCILING.value,
                    IndexGenerationState.VALIDATING.value,
                    IndexGenerationState.READY.value,
                    IndexGenerationState.FAILED.value,
                ]
            )
        )
        .all()
    )
    return {
        "ready": readiness["ready"],
        "active_generation_id": active.id if active else None,
        "previous_generation_id": previous.id if previous else None,
        "route_version": route.route_version if route else None,
        "active_fingerprint": active.embedding_fingerprint if active else None,
        "chunk_collection_ready": readiness["chunk_collection_ready"],
        "layer_collection_ready": readiness["layer_collection_ready"],
        "alias_consistent": alias_consistent,
        "write_barrier": route.write_barrier if route else None,
        "candidate_states": {item.id: item.state for item in candidates},
        "previous_lag_files": previous.mirror_lag_files if previous else None,
    }
