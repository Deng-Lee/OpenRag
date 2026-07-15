"""Fail-closed, response validation and retry tests for embeddings."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from src.openrag.chunking.chunk_models import Chunk
from src.openrag.config import EmbeddingConfig
from src.openrag.embedding.embedding_engine import EmbeddingEngine
from src.openrag.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingInputError,
    EmbeddingProviderError,
    EmbeddingResponseError,
)


class ProviderFailure(Exception):
    def __init__(self, status_code=None, retry_after=None):
        super().__init__("private provider body api-key-secret")
        self.status_code = status_code
        self.request_id = "request-123"
        self.retry_after = retry_after


class ScriptedEmbeddings:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, *, input, model):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(data=outcome)


class FakeClient:
    def __init__(self, outcomes):
        self.embeddings = ScriptedEmbeddings(outcomes)


def item(index, embedding):
    return SimpleNamespace(index=index, embedding=embedding)


def config(**overrides):
    values = {
        "provider": "openai",
        "model": "test-model",
        "dimension": 3,
        "batch_size": 8,
        "request_timeout_seconds": 60,
        "max_attempts": 3,
        "probe_interval_seconds": 30,
    }
    values.update(overrides)
    return EmbeddingConfig(**values)


def engine(outcomes, **kwargs):
    client = FakeClient(outcomes)
    instance = EmbeddingEngine(config=config(), client=client, **kwargs)
    return instance, client


def test_missing_api_key_fails_without_test_client():
    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        EmbeddingEngine(config=config(api_key=None))

    assert exc_info.value.code == "EMBEDDING_CONFIG_INVALID"


@pytest.mark.parametrize("field,value", [("model", ""), ("dimension", 0), ("batch_size", 0), ("max_attempts", 0)])
def test_invalid_configuration_fails(field, value):
    with pytest.raises(EmbeddingConfigurationError):
        EmbeddingEngine(config=config(**{field: value}), client=FakeClient([]))


def test_invalid_environment_value_maps_to_configuration_error(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "test-model")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "not-a-number")

    with pytest.raises(EmbeddingConfigurationError) as exc_info:
        EmbeddingEngine(client=FakeClient([]))

    assert exc_info.value.code == "EMBEDDING_CONFIG_INVALID"


def test_api_key_is_secret_and_not_exposed():
    cfg = config(api_key="api-key-secret")

    assert isinstance(cfg.api_key, SecretStr)
    assert "api-key-secret" not in repr(cfg)


@pytest.mark.parametrize("text", ["", " ", "\n\t", None])
def test_blank_text_is_rejected(text):
    instance, client = engine([[item(0, [1, 1, 1])]])

    with pytest.raises(EmbeddingInputError):
        instance.embed_batch([text])

    assert client.embeddings.calls == 0


def test_chunks_require_nonempty_unique_ids():
    instance, _ = engine([[item(0, [1, 1, 1])]])

    with pytest.raises(EmbeddingInputError):
        instance.embed_chunks([])
    with pytest.raises(EmbeddingInputError):
        instance.embed_chunks([Chunk(text="a", chunk_id="")])
    with pytest.raises(EmbeddingInputError):
        instance.embed_chunks([Chunk(text="a", chunk_id="same"), Chunk(text="b", chunk_id="same")])


@pytest.mark.parametrize(
    "response",
    [
        [item(0, [1, 1, 1])],
        [item(0, [1, 1, 1]), item(0, [2, 2, 2])],
        [item(0, [1, 1, 1]), item(2, [2, 2, 2])],
    ],
)
def test_incomplete_duplicate_or_out_of_range_indexes_fail(response):
    instance, _ = engine([response])

    with pytest.raises(EmbeddingResponseError):
        instance.embed_batch(["one", "two"])

    assert instance.get_cache_size() == 0


def test_out_of_order_response_is_reordered():
    instance, _ = engine([[item(1, [2, 2, 2]), item(0, [1, 1, 1])]])

    assert instance.embed_batch(["one", "two"]) == [
        [1.0, 1.0, 1.0],
        [2.0, 2.0, 2.0],
    ]


@pytest.mark.parametrize(
    "vector,code",
    [
        ([1, 1], "EMBEDDING_DIMENSION_MISMATCH"),
        ([float("nan"), 1, 1], "EMBEDDING_RESPONSE_INVALID"),
        ([float("inf"), 1, 1], "EMBEDDING_RESPONSE_INVALID"),
        ([0, 0, 0], "EMBEDDING_RESPONSE_INVALID"),
        ([True, 1, 1], "EMBEDDING_RESPONSE_INVALID"),
    ],
)
def test_invalid_vectors_fail_without_cache(vector, code):
    instance, _ = engine([[item(0, vector)]])

    with pytest.raises(EmbeddingResponseError) as exc_info:
        instance.embed_text("one")

    assert exc_info.value.code == code
    assert instance.get_cache_size() == 0


@pytest.mark.parametrize("failure", [TimeoutError(), ProviderFailure(429), ProviderFailure(500)])
def test_retryable_provider_failures_use_three_attempts(failure):
    sleeps = []
    instance, client = engine([failure], sleep_fn=sleeps.append, random_fn=lambda: 0.0)

    with pytest.raises(EmbeddingProviderError) as exc_info:
        instance.embed_text("one")

    assert exc_info.value.retryable is True
    assert client.embeddings.calls == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_provider_failures_use_one_attempt(status):
    instance, client = engine([ProviderFailure(status)], sleep_fn=lambda _: None)

    with pytest.raises(EmbeddingProviderError) as exc_info:
        instance.embed_text("one")

    assert exc_info.value.retryable is False
    assert client.embeddings.calls == 1
    assert "api-key-secret" not in exc_info.value.public_message


@pytest.mark.parametrize("retry_after,expected", [(5, 5.0), (120, 30.0)])
def test_retry_after_is_used_and_capped(retry_after, expected):
    sleeps = []
    instance, _ = engine(
        [ProviderFailure(429, retry_after), [item(0, [1, 1, 1])]],
        sleep_fn=sleeps.append,
    )

    assert instance.embed_text("one") == [1.0, 1.0, 1.0]
    assert sleeps == [expected]


def test_response_validation_failure_is_not_retried():
    instance, client = engine([[item(0, [1, 1])]])

    with pytest.raises(EmbeddingResponseError):
        instance.embed_text("one")

    assert client.embeddings.calls == 1
