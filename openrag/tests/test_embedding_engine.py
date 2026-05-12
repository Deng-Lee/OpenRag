"""Tests for embedding engine."""

import pytest
from src.openrag.embedding.embedding_engine import EmbeddingEngine
from src.openrag.chunking.chunk_models import Chunk


def test_embedding_engine_initialization():
    """Test default and custom parameters."""
    # Default initialization
    engine = EmbeddingEngine()
    assert engine.model == "text-embedding-ada-002"
    assert engine.batch_size == 100
    assert engine.cache_enabled is True

    # Custom initialization
    engine = EmbeddingEngine(
        model="text-embedding-3-small",
        batch_size=50,
        cache_enabled=False
    )
    assert engine.model == "text-embedding-3-small"
    assert engine.batch_size == 50
    assert engine.cache_enabled is False


def test_embed_single_text():
    """Test single text embedding."""
    engine = EmbeddingEngine()
    text = "Hello, world!"
    embedding = engine.embed_text(text)

    assert isinstance(embedding, list)
    assert len(embedding) > 0
    assert all(isinstance(x, float) for x in embedding)


def test_embed_text_returns_correct_dimension():
    """Verify 1536 dimensions."""
    engine = EmbeddingEngine()
    text = "Test text for embedding"
    embedding = engine.embed_text(text)

    assert len(embedding) == 1536


def test_embed_same_text_returns_same_vector():
    """Deterministic behavior."""
    engine = EmbeddingEngine()
    text = "Consistent text"

    embedding1 = engine.embed_text(text)
    embedding2 = engine.embed_text(text)

    assert embedding1 == embedding2


def test_embed_batch():
    """Test batch embedding."""
    engine = EmbeddingEngine()
    texts = ["Text 1", "Text 2", "Text 3"]
    embeddings = engine.embed_batch(texts)

    assert len(embeddings) == 3
    assert all(len(emb) == 1536 for emb in embeddings)


def test_embed_batch_empty():
    """Handle empty list."""
    engine = EmbeddingEngine()
    embeddings = engine.embed_batch([])

    assert embeddings == []


def test_embed_batch_respects_batch_size():
    """Verify batching works."""
    engine = EmbeddingEngine(batch_size=2)
    texts = ["Text 1", "Text 2", "Text 3", "Text 4", "Text 5"]
    embeddings = engine.embed_batch(texts)

    # Should process in batches of 2
    assert len(embeddings) == 5
    assert all(len(emb) == 1536 for emb in embeddings)


def test_embed_chunks():
    """Test chunk embedding."""
    engine = EmbeddingEngine()
    chunks = [
        Chunk(text="Chunk 1", chunk_id="c1"),
        Chunk(text="Chunk 2", chunk_id="c2"),
        Chunk(text="Chunk 3", chunk_id="c3")
    ]

    result = engine.embed_chunks(chunks)

    assert len(result) == 3
    for chunk, embedding in result:
        assert isinstance(chunk, Chunk)
        assert isinstance(embedding, list)
        assert len(embedding) == 1536


def test_cache_enabled():
    """Verify caching works."""
    engine = EmbeddingEngine(cache_enabled=True)
    text = "Cached text"

    # First call
    embedding1 = engine.embed_text(text)
    cache_size_after_first = engine.get_cache_size()

    # Second call should use cache
    embedding2 = engine.embed_text(text)
    cache_size_after_second = engine.get_cache_size()

    assert embedding1 == embedding2
    assert cache_size_after_first == 1
    assert cache_size_after_second == 1  # No new cache entry


def test_cache_disabled():
    """Verify cache can be disabled."""
    engine = EmbeddingEngine(cache_enabled=False)
    text = "Uncached text"

    engine.embed_text(text)
    assert engine.get_cache_size() == 0


def test_cache_hit():
    """Verify cache returns same object."""
    engine = EmbeddingEngine(cache_enabled=True)
    text = "Test cache hit"

    embedding1 = engine.embed_text(text)
    embedding2 = engine.embed_text(text)

    # Should be the exact same list object from cache
    assert embedding1 is embedding2


def test_clear_cache():
    """Test cache clearing."""
    engine = EmbeddingEngine()

    engine.embed_text("Text 1")
    engine.embed_text("Text 2")
    assert engine.get_cache_size() == 2

    engine.clear_cache()
    assert engine.get_cache_size() == 0


def test_get_cache_size():
    """Test cache size tracking."""
    engine = EmbeddingEngine()

    assert engine.get_cache_size() == 0

    engine.embed_text("Text 1")
    assert engine.get_cache_size() == 1

    engine.embed_text("Text 2")
    assert engine.get_cache_size() == 2

    # Same text should not increase cache size
    engine.embed_text("Text 1")
    assert engine.get_cache_size() == 2


def test_different_models_different_cache():
    """Different models use different cache keys."""
    engine1 = EmbeddingEngine(model="text-embedding-ada-002")
    engine2 = EmbeddingEngine(model="text-embedding-3-small")

    text = "Same text"

    embedding1 = engine1.embed_text(text)
    embedding2 = engine2.embed_text(text)

    # Different models should produce different embeddings
    # (in our mock implementation, they will be different due to different cache keys)
    assert engine1.get_cache_size() == 1
    assert engine2.get_cache_size() == 1
