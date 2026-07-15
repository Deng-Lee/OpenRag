"""Embedding failures exposed to workers and APIs through stable error codes."""

from __future__ import annotations

from typing import Optional


class EmbeddingError(Exception):
    """Base class for sanitized embedding failures."""

    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        retryable: bool = False,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds


class EmbeddingConfigurationError(EmbeddingError):
    pass


class EmbeddingInputError(EmbeddingError):
    pass


class EmbeddingProviderError(EmbeddingError):
    pass


class EmbeddingResponseError(EmbeddingError):
    pass


def public_embedding_error(exc: BaseException) -> str:
    """Return a stable message without provider response bodies or secrets."""

    if isinstance(exc, EmbeddingError):
        return exc.public_message
    return "Embedding service request failed"
