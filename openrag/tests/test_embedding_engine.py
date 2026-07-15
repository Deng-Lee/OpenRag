"""Core behavior tests for the strict embedding engine."""

from types import SimpleNamespace

from src.openrag.chunking.chunk_models import Chunk
from src.openrag.config import EmbeddingConfig
from src.openrag.embedding.embedding_engine import EmbeddingEngine


class FakeEmbeddings:
    def __init__(self, dimension: int = 3):
        self.dimension = dimension
        self.calls: list[list[str]] = []

    def create(self, *, input, model):
        self.calls.append(list(input))
        data = [
            SimpleNamespace(index=index, embedding=[float(index + 1)] * self.dimension)
            for index, _ in enumerate(input)
        ]
        return SimpleNamespace(data=data)


class FakeClient:
    def __init__(self, dimension: int = 3):
        self.embeddings = FakeEmbeddings(dimension)


def make_engine(**overrides):
    values = {
        "provider": "openai",
        "model": "test-model",
        "dimension": 3,
        "batch_size": 2,
        "request_timeout_seconds": 60,
        "max_attempts": 3,
        "probe_interval_seconds": 30,
    }
    values.update(overrides.pop("config_overrides", {}))
    client = overrides.pop("client", FakeClient(values["dimension"]))
    return EmbeddingEngine(config=EmbeddingConfig(**values), client=client, **overrides)


def test_embedding_engine_uses_resolved_configuration():
    engine = make_engine()

    assert engine.model == "test-model"
    assert engine.dimension == 3
    assert engine.batch_size == 2


def test_embed_text_and_batching():
    client = FakeClient()
    engine = make_engine(client=client)

    embedding = engine.embed_text("hello")
    batch = engine.embed_batch(["one", "two", "three"])

    assert embedding == [1.0, 1.0, 1.0]
    assert len(batch) == 3
    assert client.embeddings.calls == [["hello"], ["one", "two"], ["three"]]


def test_embed_batch_empty():
    assert make_engine().embed_batch([]) == []


def test_embed_chunks_preserves_pairing():
    chunks = [
        Chunk(text="Chunk 1", chunk_id="c1"),
        Chunk(text="Chunk 2", chunk_id="c2"),
        Chunk(text="Chunk 3", chunk_id="c3"),
    ]

    result = make_engine().embed_chunks(chunks)

    assert [chunk.chunk_id for chunk, _ in result] == ["c1", "c2", "c3"]
    assert len(result) == len(chunks)


def test_cache_is_immutable_to_callers():
    client = FakeClient()
    engine = make_engine(client=client)

    first = engine.embed_text("cached")
    first[0] = 999.0
    second = engine.embed_text("cached")

    assert second == [1.0, 1.0, 1.0]
    assert first is not second
    assert len(client.embeddings.calls) == 1


def test_cache_disabled():
    client = FakeClient()
    engine = make_engine(client=client, cache_enabled=False)

    engine.embed_text("uncached")
    engine.embed_text("uncached")

    assert engine.get_cache_size() == 0
    assert len(client.embeddings.calls) == 2


def test_clear_cache():
    engine = make_engine()
    engine.embed_text("one")
    assert engine.get_cache_size() == 1

    engine.clear_cache()

    assert engine.get_cache_size() == 0


def test_environment_changes_do_not_change_existing_engine(monkeypatch):
    engine = make_engine()

    monkeypatch.setenv("EMBEDDING_MODEL", "other-model")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "99")

    assert engine.model == "test-model"
    assert engine.dimension == 3


def test_probe_bypasses_embedding_cache():
    client = FakeClient()
    engine = make_engine(client=client)

    engine.probe()
    engine.probe()

    assert len(client.embeddings.calls) == 2
