"""Embedding engine with OpenAI API, batching, and caching.

Reads configuration from environment variables:
    OPENAI_API_KEY   – required for real embeddings (falls back to mock if unset)
    OPENAI_BASE_URL  – optional, for proxies or compatible APIs
    EMBEDDING_MODEL  – optional, defaults to text-embedding-3-small
"""

import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Dimension lookup for common OpenAI models
_MODEL_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "text-embedding-v1": 1536,
    "text-embedding-v2": 1536,
    "text-embedding-v3": 1024,
    "text-embedding-v4": 1024,
}


class EmbeddingEngine:
    """Embedding engine supporting OpenAI API with batching and caching."""

    def __init__(
        self,
        model: str | None = None,
        batch_size: int | None = None,
        cache_enabled: bool = True,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.model = model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
        self.batch_size = batch_size or int(os.environ.get("EMBEDDING_BATCH_SIZE", "8"))
        self.cache_enabled = cache_enabled
        self._cache: dict[str, list[float]] = {}

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        resolved_url = base_url or os.environ.get("OPENAI_BASE_URL")
        # 仅用于排障日志（勿记录 api_key）
        self._embed_base_url = resolved_url or "(default OpenAI API endpoint)"

        self._client = None
        if resolved_key:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=resolved_key, base_url=resolved_url)
                logger.info("EmbeddingEngine: using OpenAI model=%s", self.model)
            except ImportError:
                logger.warning("openai package not installed; falling back to mock embeddings")
        else:
            logger.warning("OPENAI_API_KEY not set; using mock embeddings")

    @property
    def dimension(self) -> int:
        env_dim = os.environ.get("EMBEDDING_DIMENSION")
        if env_dim:
            return int(env_dim)
        return _MODEL_DIMS.get(self.model, 1536)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed_text(self, text: str) -> list[float]:
        """Embed a single text string."""
        if self.cache_enabled:
            key = self._cache_key(text)
            if key in self._cache:
                return self._cache[key]

        embedding = self._embed_single(text)

        if self.cache_enabled:
            self._cache[self._cache_key(text)] = embedding
        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts, using the OpenAI batch API when available."""
        if not texts:
            return []

        results: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            results.extend(self._embed_batch(batch))
        return results

    def embed_chunks(self, chunks: list) -> list[tuple]:
        """Embed chunks and return (chunk, embedding) pairs."""
        texts = [chunk.text for chunk in chunks]
        embeddings = self.embed_batch(texts)
        return list(zip(chunks, embeddings))

    def clear_cache(self) -> None:
        self._cache.clear()

    def get_cache_size(self) -> int:
        return len(self._cache)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _embed_single(self, text: str) -> list[float]:
        if self._client is None:
            return self._mock_embed(text)
        try:
            resp = self._client.embeddings.create(input=[text], model=self.model)
            return resp.data[0].embedding
        except Exception:
            logger.exception(
                "Embedding API call failed (model=%s, base_url=%s, single); re-raising",
                self.model,
                self._embed_base_url,
            )
            raise

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch (up to self.batch_size texts)."""
        if self._client is None:
            return [self._mock_embed(t) for t in texts]

        results: list[Optional[list[float]]] = [None] * len(texts)
        to_fetch: list[tuple[int, str]] = []
        for idx, t in enumerate(texts):
            if self.cache_enabled:
                key = self._cache_key(t)
                if key in self._cache:
                    results[idx] = self._cache[key]
                    continue
            if not t or not t.strip():
                results[idx] = self._mock_embed(t)
                continue
            to_fetch.append((idx, t))

        if to_fetch:
            fetch_texts = [t for _, t in to_fetch]
            try:
                resp = self._client.embeddings.create(input=fetch_texts, model=self.model)
                resp_by_index = {item.index: item.embedding for item in resp.data}
                for fetch_pos, (orig_idx, orig_text) in enumerate(to_fetch):
                    emb = resp_by_index.get(fetch_pos)
                    if emb is not None:
                        results[orig_idx] = emb
                        if self.cache_enabled:
                            self._cache[self._cache_key(orig_text)] = emb
                    else:
                        logger.warning("No embedding returned for index %d, using mock", fetch_pos)
                        results[orig_idx] = self._mock_embed(orig_text)
            except Exception:
                logger.exception(
                    "Embedding API call failed (model=%s, base_url=%s, items=%d); falling back to mock",
                    self.model,
                    self._embed_base_url,
                    len(fetch_texts),
                )
                for orig_idx, orig_text in to_fetch:
                    if results[orig_idx] is None:
                        results[orig_idx] = self._mock_embed(orig_text)

        for i, r in enumerate(results):
            if r is None:
                results[i] = self._mock_embed(texts[i])

        return results  # type: ignore[return-value]

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest()

    def _mock_embed(self, text: str) -> list[float]:
        """Deterministic mock embedding for testing when no API key is set."""
        h = hashlib.sha256(text.encode()).digest()
        dim = self.dimension
        return [((h[i % len(h)] / 255.0) * 2 - 1) for i in range(dim)]
