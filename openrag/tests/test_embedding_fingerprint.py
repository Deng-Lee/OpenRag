"""Stable embedding identity fingerprint contracts."""

from copy import deepcopy

from pydantic import SecretStr

from openrag.config import EmbeddingConfig
from openrag.indexing.fingerprint import (
    build_embedding_manifest,
    compute_embedding_fingerprint,
    normalize_embedding_manifest,
)


def config(**overrides) -> EmbeddingConfig:
    values = {
        "provider": "openai",
        "model": "Qwen3-Embedding-4B",
        "revision": "sha256:weights-v1",
        "model_identity": "registry/qwen3@sha256:weights-v1",
        "dimension": 2560,
        "normalization": "none",
        "input_type": "text",
        "encoding_format": "float",
        "distance_metric": "COSINE",
        "query_prefix_revision": "query-v1",
        "document_prefix_revision": "document-v1",
        "text_preprocess_revision": "text-v1",
        "sdk_contract_revision": "openai-v1",
        "config_ref": "embedding/default",
        "api_key": SecretStr("secret-a"),
    }
    values.update(overrides)
    return EmbeddingConfig(**values)


def test_field_order_does_not_change_fingerprint():
    manifest = build_embedding_manifest(config())
    reversed_manifest = dict(reversed(list(manifest.items())))

    assert compute_embedding_fingerprint(manifest) == compute_embedding_fingerprint(
        reversed_manifest
    )


def test_secrets_and_transient_runtime_settings_do_not_change_fingerprint():
    original = config(
        api_key=SecretStr("secret-a"),
        batch_size=8,
        request_timeout_seconds=30,
        max_attempts=2,
    )
    transient_changed = config(
        api_key=SecretStr("secret-b"),
        batch_size=64,
        request_timeout_seconds=120,
        max_attempts=5,
    )

    assert compute_embedding_fingerprint(original) == compute_embedding_fingerprint(
        transient_changed
    )


def test_each_model_identity_field_changes_fingerprint():
    original = config()
    original_fingerprint = compute_embedding_fingerprint(original)
    changes = {
        "provider": "other-provider",
        "model": "other-model",
        "revision": "sha256:weights-v2",
        "model_identity": "registry/qwen3@sha256:weights-v2",
        "dimension": 1024,
        "normalization": "l2",
        "input_type": "document",
        "encoding_format": "base64",
        "distance_metric": "IP",
        "query_prefix_revision": "query-v2",
        "document_prefix_revision": "document-v2",
        "text_preprocess_revision": "text-v2",
        "sdk_contract_revision": "openai-v2",
    }

    for field, value in changes.items():
        assert (
            compute_embedding_fingerprint(config(**{field: value}))
            != original_fingerprint
        )


def test_same_dimension_different_model_identity_is_not_compatible():
    first = config(model_identity="digest-a")
    second = config(model_identity="digest-b")

    assert first.dimension == second.dimension
    assert compute_embedding_fingerprint(first) != compute_embedding_fingerprint(second)


def test_normalized_manifest_contains_no_credentials_or_endpoint():
    embedding_config = config(base_url="https://secret-gateway.internal/v1")
    manifest = normalize_embedding_manifest(build_embedding_manifest(embedding_config))

    assert "api_key" not in manifest
    assert "base_url" not in manifest
    assert "secret-a" not in str(manifest)
    assert "secret-gateway" not in str(manifest)


def test_manifest_normalization_does_not_mutate_input():
    manifest = build_embedding_manifest(config())
    original = deepcopy(manifest)

    normalize_embedding_manifest(manifest)

    assert manifest == original
