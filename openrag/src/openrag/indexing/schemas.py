"""Validated, credential-free schemas for the index generation control plane."""

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field

from openrag.config import EmbeddingConfig
from openrag.indexing.fingerprint import (
    build_embedding_manifest,
    compute_embedding_fingerprint,
    normalize_embedding_manifest,
)
from openrag.services.index_generation_service import IndexGenerationService


class GenerationCreateRequest(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    embedding_config_ref: str = Field(min_length=1, max_length=256)
    embedding_model: str = Field(min_length=1, max_length=256)
    embedding_revision: str = Field(min_length=1, max_length=256)
    embedding_dimension: int = Field(gt=0)
    schema_version: int = Field(default=2, ge=1)
    chunk_policy_revision: str = Field(min_length=1, max_length=128)
    hierarchy_policy_revision: str = Field(min_length=1, max_length=128)
    rollback_window_seconds: int = Field(default=86400, ge=0)
    force_rebuild: bool = False


class ProvisionRequest(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)


class ReindexRequest(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    enqueue_limit: int = Field(default=100, ge=1, le=1000)
    max_concurrent: int = Field(default=4, ge=1, le=64)


class ActivationRequest(ProvisionRequest):
    expected_route_version: int = Field(ge=0)


class RollbackRequest(ActivationRequest):
    accept_rpo: bool = False


class GenerationPlan(BaseModel):
    generation_id: str
    manifest: dict[str, Any]
    database_values: dict[str, Any]
    physical_collections: dict[str, str]


def build_generation_plan(
    request: GenerationCreateRequest,
    base_config: EmbeddingConfig,
    source_generation_id: str | None = None,
) -> GenerationPlan:
    generation_id = str(
        uuid5(NAMESPACE_URL, f"openrag:index-generation:{request.client_request_id}")
    )
    chunk_name, layer_name = IndexGenerationService.reserve_collection_names(
        generation_id
    )
    candidate_config = base_config.model_copy(
        update={
            "model": request.embedding_model,
            "revision": request.embedding_revision,
            "dimension": request.embedding_dimension,
            "config_ref": request.embedding_config_ref,
        }
    )
    embedding = normalize_embedding_manifest(build_embedding_manifest(candidate_config))
    fingerprint = compute_embedding_fingerprint(embedding)
    manifest = {
        "generation_id": generation_id,
        "scope": "global",
        "embedding": embedding,
        "source_generation_id": source_generation_id,
        "embedding_provider": embedding["provider"],
        "embedding_model": embedding["model"],
        "embedding_revision": embedding["model_revision"],
        "embedding_dimension": embedding["dimension"],
        "embedding_fingerprint": fingerprint,
        "embedding_config_ref": request.embedding_config_ref,
        "schema_version": request.schema_version,
        "chunk_policy_revision": request.chunk_policy_revision,
        "hierarchy_policy_revision": request.hierarchy_policy_revision,
        "chunk_collection_name": chunk_name,
        "layer_collection_name": layer_name,
        "rollback_window_seconds": request.rollback_window_seconds,
        "force_rebuild": request.force_rebuild,
    }
    values = {
        "id": generation_id,
        "scope": "global",
        "source_generation_id": source_generation_id,
        "embedding_provider": embedding["provider"],
        "embedding_model": embedding["model"],
        "embedding_revision": embedding["model_revision"],
        "embedding_dimension": embedding["dimension"],
        "embedding_fingerprint": fingerprint,
        "embedding_config_ref": request.embedding_config_ref,
        "vector_normalization": embedding["normalization"],
        "distance_metric": embedding["distance_metric"],
        "schema_version": request.schema_version,
        "chunk_policy_revision": request.chunk_policy_revision,
        "hierarchy_policy_revision": request.hierarchy_policy_revision,
        "chunk_collection_name": chunk_name,
        "layer_collection_name": layer_name,
        "manifest": manifest,
    }
    return GenerationPlan(
        generation_id=generation_id,
        manifest=manifest,
        database_values=values,
        physical_collections={"chunks": chunk_name, "layers": layer_name},
    )
