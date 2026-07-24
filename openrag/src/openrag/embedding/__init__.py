"""Embedding module for OpenRag."""

from .embedding_engine import (
    EmbeddingEngine,
    initialize_embedding_dependency,
    validate_embedding_config,
)
from .errors import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingInputError,
    EmbeddingProviderError,
    EmbeddingResponseError,
    public_embedding_error,
)
from openrag.indexing.fingerprint import (
    build_embedding_manifest,
    compute_embedding_fingerprint,
    normalize_embedding_manifest,
    resolve_model_identity,
)

__all__ = [
    "EmbeddingConfigurationError",
    "EmbeddingEngine",
    "EmbeddingError",
    "EmbeddingInputError",
    "EmbeddingProviderError",
    "EmbeddingResponseError",
    "initialize_embedding_dependency",
    "public_embedding_error",
    "validate_embedding_config",
    "build_embedding_manifest",
    "compute_embedding_fingerprint",
    "normalize_embedding_manifest",
    "resolve_model_identity",
]
