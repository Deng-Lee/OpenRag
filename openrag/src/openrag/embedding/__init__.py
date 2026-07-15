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
]
