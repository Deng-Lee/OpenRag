"""Canonical, credential-free identity for an embedding vector space."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from openrag.config import EmbeddingConfig

_MANIFEST_FIELDS = (
    "provider",
    "model",
    "model_revision",
    "model_identity",
    "dimension",
    "input_type",
    "encoding_format",
    "normalization",
    "distance_metric",
    "query_prefix_revision",
    "document_prefix_revision",
    "text_preprocess_revision",
    "sdk_contract_revision",
)


def resolve_model_identity(config: EmbeddingConfig) -> str:
    explicit = config.model_identity.strip()
    if explicit:
        return explicit
    revision = config.revision.strip()
    suffix = f"@{revision}" if revision else ""
    return f"{config.provider.strip().lower()}:{config.model.strip()}{suffix}"


def build_embedding_manifest(config: EmbeddingConfig) -> dict[str, Any]:
    """Build only vector-space identity fields; never include credentials or endpoints."""

    return {
        "provider": config.provider,
        "model": config.model,
        "model_revision": config.revision,
        "model_identity": resolve_model_identity(config),
        "dimension": config.dimension,
        "input_type": config.input_type,
        "encoding_format": config.encoding_format,
        "normalization": config.normalization,
        "distance_metric": config.distance_metric,
        "query_prefix_revision": config.query_prefix_revision,
        "document_prefix_revision": config.document_prefix_revision,
        "text_preprocess_revision": config.text_preprocess_revision,
        "sdk_contract_revision": config.sdk_contract_revision,
    }


def normalize_embedding_manifest(
    manifest: EmbeddingConfig | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(manifest, EmbeddingConfig):
        values = build_embedding_manifest(manifest)
    else:
        values = dict(manifest)
    missing = [field for field in _MANIFEST_FIELDS if field not in values]
    if missing:
        raise ValueError(f"Embedding manifest is missing fields: {missing}")

    normalized = {field: values[field] for field in _MANIFEST_FIELDS}
    normalized["provider"] = str(normalized["provider"]).strip().lower()
    normalized["model"] = str(normalized["model"]).strip()
    normalized["model_revision"] = str(normalized["model_revision"] or "").strip()
    normalized["model_identity"] = str(normalized["model_identity"]).strip()
    normalized["dimension"] = int(normalized["dimension"])
    normalized["input_type"] = str(normalized["input_type"]).strip().lower()
    normalized["encoding_format"] = str(normalized["encoding_format"]).strip().lower()
    normalized["normalization"] = str(normalized["normalization"]).strip().lower()
    normalized["distance_metric"] = str(normalized["distance_metric"]).strip().upper()
    for field in (
        "query_prefix_revision",
        "document_prefix_revision",
        "text_preprocess_revision",
        "sdk_contract_revision",
    ):
        normalized[field] = str(normalized[field] or "").strip()
    return normalized


def compute_embedding_fingerprint(
    manifest: EmbeddingConfig | Mapping[str, Any],
) -> str:
    normalized = normalize_embedding_manifest(manifest)
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
