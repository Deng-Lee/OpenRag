import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from openrag.api import search_api
from openrag.api.search_api import SearchRequest
from openrag.retrieval.retrieval_service import PermissionScopeResolutionError


class TestSearchRequestVectorSimilarityWeight:
    def test_vector_similarity_weight_defaults_to_0_7(self):
        assert SearchRequest(query="test").vector_similarity_weight == 0.7

    @pytest.mark.parametrize("weight", [0.0, 0.7, 1.0])
    def test_vector_similarity_weight_accepts_valid_values(self, weight):
        assert SearchRequest(query="test", vector_similarity_weight=weight).vector_similarity_weight == weight

    @pytest.mark.parametrize("weight", [-0.1, 1.1])
    def test_vector_similarity_weight_rejects_out_of_range_values(self, weight):
        with pytest.raises(ValidationError):
            SearchRequest(query="test", vector_similarity_weight=weight)


def _patch_execute_dependencies(monkeypatch, results):
    trace_service = Mock()
    retrieval_search = Mock(return_value=[dict(result) for result in results])
    monkeypatch.setattr(
        search_api, "_prepare_retrieval_trace", lambda **kwargs: (trace_service, False)
    )
    monkeypatch.setattr(search_api, "_get_embedding_engine", Mock(return_value=Mock()))
    monkeypatch.setattr(search_api, "_get_vector_store", Mock(return_value=Mock()))
    monkeypatch.setattr(search_api, "_get_layer_store", Mock(return_value=None))
    monkeypatch.setattr(search_api, "_get_fulltext_store", Mock(return_value=None))
    monkeypatch.setattr(
        search_api,
        "RetrievalService",
        lambda **kwargs: SimpleNamespace(search=retrieval_search),
    )
    return retrieval_search


def test_execute_search_preserves_request_and_response_contract(monkeypatch):
    search = _patch_execute_dependencies(
        monkeypatch,
        [
            {
                "text": "result",
                "score": 0.9,
                "file_id": 3,
                "chunk_id": "chunk-3",
                "page": 2,
                "filename": "doc.pdf",
            }
        ],
    )
    request = SearchRequest(
        query="q",
        top_k=5,
        workspace_id=7,
        use_rerank=False,
        vector_similarity_weight=0.3,
    )

    response = search_api._execute_search(
        Mock(spec=Session),
        user_id=1,
        request=request,
        endpoint="semantic",
        rerank_hierarchical_boost=None,
    )

    assert response.total == 1
    assert response.results[0].model_dump() == {
        "text": "result",
        "score": 0.9,
        "file_id": 3,
        "chunk_id": "chunk-3",
        "chunk_index": None,
        "page": 2,
        "level": 0,
        "block_type": "text",
        "start_offset": 0,
        "end_offset": 0,
        "bbox_x0": None,
        "bbox_y0": None,
        "bbox_x1": None,
        "bbox_y1": None,
        "source_block_id": None,
        "source_char_start": None,
        "source_char_end": None,
        "filename": "doc.pdf",
        "uri": None,
        "object_key": None,
        "object_url": None,
        "local_chunk_path": None,
        "text_preview": None,
        "retrieval_strategy": None,
        "l1_llm_filtered": None,
    }
    assert search.call_args.kwargs["vector_similarity_weight"] == 0.3
    assert search.call_args.kwargs["top_k"] == 5


def test_execute_search_reranks_union_before_top_k(monkeypatch):
    results = [
        {"text": f"r{i}", "score": 0.9 - i * 0.1, "file_id": i + 1, "chunk_id": f"c{i}"}
        for i in range(4)
    ]
    search = _patch_execute_dependencies(monkeypatch, results)
    rerank = Mock(return_value=[{**results[3], "reranked_score": 0.99}])
    monkeypatch.setattr(
        search_api, "Reranker", lambda **kwargs: SimpleNamespace(rerank=rerank)
    )

    response = search_api._execute_search(
        Mock(spec=Session),
        user_id=1,
        request=SearchRequest(query="q", top_k=1, use_rerank=True),
        endpoint="semantic",
        rerank_hierarchical_boost=None,
    )

    assert search.call_args.kwargs["top_k"] == 3
    assert rerank.call_args.args[1] == results
    assert response.results[0].chunk_id == "c3"
    assert response.results[0].score == 0.99


def test_permission_scope_resolution_error_returns_explicit_503(monkeypatch):
    _patch_execute_dependencies(monkeypatch, [])
    monkeypatch.setattr(
        search_api,
        "RetrievalService",
        lambda **kwargs: SimpleNamespace(
            search=Mock(side_effect=PermissionScopeResolutionError("internal detail"))
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        search_api._execute_search(
            Mock(spec=Session),
            user_id=1,
            request=SearchRequest(query="q", workspace_id=7),
            endpoint="semantic",
            rerank_hierarchical_boost=None,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "permission_scope_resolution_failed",
        "message": "Unable to verify search permissions",
    }


def test_format_score_prefers_reranker_then_fusion_then_single_channel():
    formatted = search_api._format(
        [
            {"text": "reranked", "file_id": 1, "score": 0.9, "fused_score": 0.2, "reranked_score": 0.8},
            {"text": "fused", "file_id": 2, "score": 0.7, "fused_score": 0.3},
            {"text": "dense", "file_id": 3, "score": 0.6},
        ]
    )
    assert [item.score for item in formatted] == [0.8, 0.3, 0.6]


def test_semantic_endpoint_maps_unexpected_error_to_500(monkeypatch):
    monkeypatch.setattr(search_api, "assert_search_workspace_read", Mock())
    monkeypatch.setattr(
        search_api, "_execute_search", Mock(side_effect=RuntimeError("search unavailable"))
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            search_api.semantic_search(
                SearchRequest(query="q"), user_id=1, db=Mock(spec=Session)
            )
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "search unavailable"


@pytest.mark.parametrize("payload", [{"query": ""}, {"query": "q", "top_k": 0}])
def test_invalid_search_request_is_rejected(payload):
    with pytest.raises(ValidationError):
        SearchRequest(**payload)
