"""Search API maps embedding failures to stable public HTTP errors."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from unittest.mock import Mock

from openrag.api import search_api
from openrag.api.search_api import (
    SearchRequest,
    _embedding_error_to_http_exception,
)
from openrag.retrieval.retrieval_service import ResolvedFileScope
from openrag.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingInputError,
    EmbeddingProviderError,
    EmbeddingResponseError,
)


def test_query_is_trimmed():
    assert SearchRequest(query="  hello  ").query == "hello"


def test_blank_query_is_rejected_before_embedding():
    with pytest.raises(ValidationError):
        SearchRequest(query=" \n\t ")


@pytest.mark.parametrize(
    "error,status_code,code",
    [
        (
            EmbeddingInputError("EMBEDDING_INPUT_INVALID", "private"),
            422,
            "embedding_input_invalid",
        ),
        (
            EmbeddingProviderError(
                "EMBEDDING_TIMEOUT", "private provider body", retryable=True
            ),
            503,
            "embedding_service_unavailable",
        ),
        (
            EmbeddingConfigurationError("EMBEDDING_CONFIG_INVALID", "private key"),
            503,
            "embedding_configuration_error",
        ),
        (
            EmbeddingResponseError("EMBEDDING_RESPONSE_INVALID", "private response"),
            502,
            "embedding_response_invalid",
        ),
    ],
)
def test_embedding_http_mapping_is_structured_and_sanitized(error, status_code, code):
    http_error = _embedding_error_to_http_exception(error)

    assert http_error.status_code == status_code
    assert http_error.detail["code"] == code
    assert "private" not in http_error.detail["message"]


class FakeRetrievalService:
    def __init__(self, **kwargs):
        self.embedding_engine = kwargs.get("embedding_engine")
        self.vector_store = kwargs.get("vector_store")
        self.layer_store = kwargs.get("layer_store")
        self.fulltext_store = kwargs.get("fulltext_store")

    def resolve_file_scope(self, *args):
        return ResolvedFileScope(None)

    def search(self, **kwargs):
        return []


def configure_execute_search(monkeypatch):
    monkeypatch.setattr(search_api, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(search_api, "assert_search_workspace_read", lambda *args: None)
    monkeypatch.setattr(
        search_api, "_prepare_retrieval_trace", lambda **kwargs: (Mock(), False)
    )
    monkeypatch.setattr(search_api, "_get_embedding_engine", lambda: object())
    monkeypatch.setattr(search_api, "_get_vector_store", lambda: object())
    monkeypatch.setattr(search_api, "_get_fulltext_store", lambda: None)


def test_flat_search_does_not_initialize_layer_store(monkeypatch):
    configure_execute_search(monkeypatch)
    monkeypatch.setattr(search_api, "l0_l1_retrieval_enabled", lambda: True)
    monkeypatch.setattr(
        search_api,
        "_get_layer_store",
        lambda: pytest.fail("flat search must not initialize layer store"),
    )

    response = search_api._execute_search(
        Mock(),
        1,
        SearchRequest(query="query", use_rerank=False),
        endpoint="semantic",
        rerank_hierarchical_boost=None,
    )

    assert response.results == []


def test_explicit_hierarchical_search_does_not_fall_back_when_disabled(monkeypatch):
    configure_execute_search(monkeypatch)
    monkeypatch.setattr(search_api, "l0_l1_retrieval_enabled", lambda: False)

    with pytest.raises(HTTPException) as exc_info:
        search_api._execute_search(
            Mock(),
            1,
            SearchRequest(query="query"),
            endpoint="hierarchical",
            rerank_hierarchical_boost=0.15,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "hierarchical_index_unavailable"
