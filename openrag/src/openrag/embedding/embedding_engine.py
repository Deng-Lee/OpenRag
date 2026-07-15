"""Strict OpenAI-compatible embedding client with validation and bounded retries."""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Callable, Optional

from pydantic import ValidationError

from ..config import EmbeddingConfig
from .errors import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingInputError,
    EmbeddingProviderError,
    EmbeddingResponseError,
)

logger = logging.getLogger(__name__)


def validate_embedding_config(
    config: EmbeddingConfig, *, require_api_key: bool = True
) -> None:
    """Validate local configuration before contacting the provider."""

    if config.provider.strip().lower() != "openai":
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Unsupported embedding provider"
        )
    if not config.model or not config.model.strip():
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding model is not configured"
        )
    if require_api_key and (
        config.api_key is None or not config.api_key.get_secret_value().strip()
    ):
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding API key is not configured"
        )
    if config.dimension <= 0:
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding dimension must be greater than zero"
        )
    if not 1 <= config.batch_size <= 2048:
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding batch size must be between 1 and 2048"
        )
    if not 1 <= config.request_timeout_seconds <= 600:
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding timeout must be between 1 and 600 seconds"
        )
    if not 1 <= config.max_attempts <= 5:
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding max attempts must be between 1 and 5"
        )
    if not 5 <= config.probe_interval_seconds <= 300:
        raise EmbeddingConfigurationError(
            "EMBEDDING_CONFIG_INVALID", "Embedding probe interval must be between 5 and 300 seconds"
        )


def _read_attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _extract_status_code(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = _read_attr(getattr(exc, "response", None), "status_code")
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _extract_request_id(exc: BaseException) -> Optional[str]:
    request_id = getattr(exc, "request_id", None)
    if request_id:
        return str(request_id)
    response = getattr(exc, "response", None)
    headers = _read_attr(response, "headers", {}) or {}
    return headers.get("x-request-id") or headers.get("request-id")


def _extract_retry_after(exc: BaseException) -> Optional[float]:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        response = getattr(exc, "response", None)
        headers = _read_attr(response, "headers", {}) or {}
        retry_after = headers.get("retry-after")
    try:
        value = float(retry_after)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(value, 30.0))


def _classify_provider_error(exc: BaseException) -> EmbeddingError:
    if isinstance(exc, EmbeddingError):
        return exc

    status_code = _extract_status_code(exc)
    request_id = _extract_request_id(exc)
    retry_after = _extract_retry_after(exc)
    name = type(exc).__name__.lower()

    if "timeout" in name or isinstance(exc, TimeoutError):
        code, message, retryable = (
            "EMBEDDING_TIMEOUT",
            "Embedding service request timed out",
            True,
        )
    elif status_code == 429:
        code, message, retryable = (
            "EMBEDDING_RATE_LIMITED",
            "Embedding service rate limit exceeded",
            True,
        )
    elif status_code in {408, 409} or (status_code is not None and status_code >= 500):
        code, message, retryable = (
            "EMBEDDING_PROVIDER_UNAVAILABLE",
            "Embedding service temporarily unavailable",
            True,
        )
    elif status_code in {401, 403}:
        code, message, retryable = (
            "EMBEDDING_AUTH_FAILED",
            "Embedding service authentication failed",
            False,
        )
    elif status_code in {400, 404, 422}:
        code, message, retryable = (
            "EMBEDDING_REQUEST_REJECTED",
            "Embedding service rejected the request",
            False,
        )
    elif "connection" in name or isinstance(exc, ConnectionError):
        code, message, retryable = (
            "EMBEDDING_PROVIDER_UNAVAILABLE",
            "Embedding service temporarily unavailable",
            True,
        )
    else:
        code, message, retryable = (
            "EMBEDDING_PROVIDER_UNAVAILABLE",
            "Embedding service request failed",
            False,
        )

    return EmbeddingProviderError(
        code,
        message,
        retryable=retryable,
        status_code=status_code,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


class EmbeddingEngine:
    """Embedding engine with fail-closed validation, batching and caching."""

    def __init__(
        self,
        model: str | None = None,
        batch_size: int | None = None,
        cache_enabled: bool = True,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        config: EmbeddingConfig | None = None,
        client: Any = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        try:
            resolved = config or EmbeddingConfig()
        except ValidationError as exc:
            raise EmbeddingConfigurationError(
                "EMBEDDING_CONFIG_INVALID", "Embedding configuration is invalid"
            ) from exc
        updates: dict[str, Any] = {}
        if model is not None:
            updates["model"] = model
        if batch_size is not None:
            updates["batch_size"] = batch_size
        if api_key is not None:
            updates["api_key"] = api_key
        if base_url is not None:
            updates["base_url"] = base_url
        if updates:
            try:
                resolved = EmbeddingConfig.model_validate(
                    {**resolved.model_dump(), **updates}
                )
            except ValidationError as exc:
                raise EmbeddingConfigurationError(
                    "EMBEDDING_CONFIG_INVALID", "Embedding configuration is invalid"
                ) from exc

        validate_embedding_config(resolved, require_api_key=client is None)
        self.config = resolved
        self.provider = resolved.provider.strip().lower()
        self.model = resolved.model.strip()
        self.batch_size = resolved.batch_size
        self.cache_enabled = cache_enabled
        self._dimension = resolved.dimension
        self._base_url = (resolved.base_url or "").rstrip("/")
        self._cache: dict[str, tuple[float, ...]] = {}
        self._sleep = sleep_fn
        self._random = random_fn
        self._health: dict[str, Any] = {
            "last_success_at": None,
            "last_failure_at": None,
            "last_probe_at": None,
            "last_error_code": None,
            "ready": False,
        }

        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise EmbeddingConfigurationError(
                    "EMBEDDING_CONFIG_INVALID", "OpenAI SDK is not installed"
                ) from exc
            self._client = OpenAI(
                api_key=resolved.api_key.get_secret_value(),
                base_url=resolved.base_url,
                timeout=resolved.request_timeout_seconds,
                max_retries=0,
            )
        logger.info(
            "embedding_dependency_initialized provider=%s model=%s dimension=%d",
            self.provider,
            self.model,
            self.dimension,
        )

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_text(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._validate_texts(texts)

        results: list[Optional[list[float]]] = [None] * len(texts)
        misses: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            cached = self._cache.get(self._cache_key(text)) if self.cache_enabled else None
            if cached is None:
                misses.append((index, text))
            else:
                results[index] = list(cached)

        for start in range(0, len(misses), self.batch_size):
            batch = misses[start : start + self.batch_size]
            vectors = self._request_embeddings([text for _, text in batch])
            if len(vectors) != len(batch):
                raise EmbeddingResponseError(
                    "EMBEDDING_RESPONSE_INVALID",
                    "Embedding service returned an incomplete response",
                )
            for (original_index, _), vector in zip(batch, vectors):
                results[original_index] = vector

        if any(vector is None for vector in results):
            raise EmbeddingResponseError(
                "EMBEDDING_RESPONSE_INVALID",
                "Embedding service returned an incomplete response",
            )

        completed = [list(vector) for vector in results if vector is not None]
        if self.cache_enabled:
            for text, vector in zip(texts, completed):
                self._cache[self._cache_key(text)] = tuple(vector)
        return completed

    def embed_chunks(self, chunks: list) -> list[tuple]:
        if not chunks:
            raise EmbeddingInputError(
                "EMBEDDING_INPUT_INVALID", "At least one chunk is required"
            )
        chunk_ids = [getattr(chunk, "chunk_id", None) for chunk in chunks]
        if any(not str(chunk_id or "").strip() for chunk_id in chunk_ids):
            raise EmbeddingInputError(
                "EMBEDDING_INPUT_INVALID", "Every chunk must have a chunk ID"
            )
        if len(set(chunk_ids)) != len(chunk_ids):
            raise EmbeddingInputError(
                "EMBEDDING_INPUT_INVALID", "Chunk IDs must be unique"
            )
        embeddings = self.embed_batch([getattr(chunk, "text", None) for chunk in chunks])
        if len(embeddings) != len(chunks):
            raise EmbeddingResponseError(
                "EMBEDDING_RESPONSE_INVALID",
                "Embedding count does not match chunk count",
            )
        return list(zip(chunks, embeddings))

    def probe(self) -> dict[str, Any]:
        self._health["last_probe_at"] = self._now()
        try:
            self._request_embeddings(["openrag embedding dependency probe"])
        except EmbeddingError as exc:
            self._record_failure(exc)
            logger.warning(
                "embedding_probe_failure provider=%s model=%s error_code=%s",
                self.provider,
                self.model,
                exc.code,
            )
            raise
        self._record_success()
        logger.info(
            "embedding_probe_success provider=%s model=%s dimension=%d",
            self.provider,
            self.model,
            self.dimension,
        )
        return self.health_snapshot()

    def health_snapshot(self) -> dict[str, Any]:
        return dict(self._health)

    def clear_cache(self) -> None:
        self._cache.clear()

    def get_cache_size(self) -> int:
        return len(self._cache)

    def _validate_texts(self, texts: list[str]) -> None:
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise EmbeddingInputError(
                    "EMBEDDING_INPUT_INVALID", "Embedding text must not be blank"
                )

    def _validate_embedding(self, vector: Any) -> list[float]:
        if not isinstance(vector, (list, tuple)) or len(vector) != self.dimension:
            raise EmbeddingResponseError(
                "EMBEDDING_DIMENSION_MISMATCH",
                "Embedding vector dimension does not match configuration",
            )
        values: list[float] = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, Real):
                raise EmbeddingResponseError(
                    "EMBEDDING_RESPONSE_INVALID",
                    "Embedding service returned invalid vector values",
                )
            number = float(value)
            if not math.isfinite(number):
                raise EmbeddingResponseError(
                    "EMBEDDING_RESPONSE_INVALID",
                    "Embedding service returned invalid vector values",
                )
            values.append(number)
        if math.sqrt(sum(value * value for value in values)) <= 1e-12:
            raise EmbeddingResponseError(
                "EMBEDDING_RESPONSE_INVALID",
                "Embedding service returned a zero vector",
            )
        return values

    def _validate_response(self, response_data: Any, expected_count: int) -> list[list[float]]:
        try:
            items = list(response_data)
        except TypeError as exc:
            raise EmbeddingResponseError(
                "EMBEDDING_RESPONSE_INVALID", "Embedding service returned an invalid response"
            ) from exc
        if len(items) != expected_count:
            raise EmbeddingResponseError(
                "EMBEDDING_RESPONSE_INVALID",
                "Embedding service returned an incomplete response",
            )
        ordered: list[Optional[list[float]]] = [None] * expected_count
        for item in items:
            index = _read_attr(item, "index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise EmbeddingResponseError(
                    "EMBEDDING_RESPONSE_INVALID", "Embedding response index is invalid"
                )
            if index < 0 or index >= expected_count or ordered[index] is not None:
                raise EmbeddingResponseError(
                    "EMBEDDING_RESPONSE_INVALID", "Embedding response index is invalid"
                )
            ordered[index] = self._validate_embedding(_read_attr(item, "embedding"))
        if any(vector is None for vector in ordered):
            raise EmbeddingResponseError(
                "EMBEDDING_RESPONSE_INVALID",
                "Embedding service returned an incomplete response",
            )
        return [vector for vector in ordered if vector is not None]

    def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(1, self.config.max_attempts + 1):
            started = time.monotonic()
            try:
                response = self._client.embeddings.create(input=texts, model=self.model)
                vectors = self._validate_response(_read_attr(response, "data"), len(texts))
                self._record_success()
                logger.info(
                    "embedding_request_success provider=%s model=%s input_count=%d attempt=%d latency_ms=%d",
                    self.provider,
                    self.model,
                    len(texts),
                    attempt,
                    int((time.monotonic() - started) * 1000),
                )
                return vectors
            except EmbeddingResponseError as exc:
                self._record_failure(exc)
                logger.warning(
                    "embedding_validation_failure provider=%s model=%s input_count=%d error_code=%s",
                    self.provider,
                    self.model,
                    len(texts),
                    exc.code,
                )
                raise
            except Exception as exc:
                error = _classify_provider_error(exc)
                self._record_failure(error)
                if not error.retryable or attempt >= self.config.max_attempts:
                    logger.warning(
                        "embedding_request_failure provider=%s model=%s input_count=%d attempt=%d error_code=%s status_code=%s request_id=%s",
                        self.provider,
                        self.model,
                        len(texts),
                        attempt,
                        error.code,
                        error.status_code,
                        error.request_id,
                    )
                    raise error from exc
                delay = self._calculate_retry_delay(attempt, error.retry_after_seconds)
                logger.warning(
                    "embedding_request_retry provider=%s model=%s input_count=%d attempt=%d error_code=%s delay_seconds=%.3f",
                    self.provider,
                    self.model,
                    len(texts),
                    attempt,
                    error.code,
                    delay,
                )
                self._sleep(delay)
        raise AssertionError("unreachable")

    def _calculate_retry_delay(
        self, attempt: int, retry_after_seconds: Optional[float]
    ) -> float:
        if retry_after_seconds is not None:
            return min(max(retry_after_seconds, 0.0), 30.0)
        return min(30.0, (2 ** (attempt - 1)) + self._random())

    def _cache_key(self, text: str) -> str:
        raw = f"{self.provider}:{self._base_url}:{self.model}:{self.dimension}:{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _record_success(self) -> None:
        self._health["last_success_at"] = self._now()
        self._health["last_error_code"] = None
        self._health["ready"] = True

    def _record_failure(self, exc: EmbeddingError) -> None:
        self._health["last_failure_at"] = self._now()
        self._health["last_error_code"] = exc.code
        self._health["ready"] = False

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


def initialize_embedding_dependency(
    config: EmbeddingConfig | None = None,
) -> EmbeddingEngine:
    return EmbeddingEngine(config=config)
