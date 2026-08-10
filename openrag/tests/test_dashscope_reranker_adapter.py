"""Tests for DashScope API-based reranker adapter."""

from unittest.mock import Mock

import pytest

from openrag.retrieval.reranker import (
    DashScopeRerankAdapter,
    LiteLLMRerankAdapter,
    Reranker,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_litellm_adapter_sends_flat_rerank_payload(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "results": [
                    {"index": 1, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.23},
                ]
            }
        )

    monkeypatch.setattr("openrag.retrieval.reranker.requests.post", fake_post)
    adapter = LiteLLMRerankAdapter(
        api_key="test-key",
        base_url="https://litellm.example/rerank",
        model="Qwen3-VL-Reranker-8B",
        timeout=12.0,
    )

    scores = adapter.score(
        "银行卡绑卡",
        [{"text": "银行卡绑定说明"}, {"text": "信用卡还款说明"}],
    )

    assert scores == [0.23, 0.91]
    assert captured["url"] == "https://litellm.example/rerank"
    assert captured["timeout"] == 12.0
    assert adapter.max_documents == 100
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"] == {
        "model": "Qwen3-VL-Reranker-8B",
        "query": "银行卡绑卡",
        "documents": ["银行卡绑定说明", "信用卡还款说明"],
        "top_n": 2,
        "return_documents": False,
    }


def test_litellm_adapter_rejects_incomplete_score_response(monkeypatch):
    monkeypatch.setattr(
        "openrag.retrieval.reranker.requests.post",
        lambda *args, **kwargs: _FakeResponse(
            {"results": [{"index": 0, "relevance_score": 0.23}]}
        ),
    )
    adapter = LiteLLMRerankAdapter(
        api_key="test-key",
        base_url="https://litellm.example/rerank",
        model="Qwen3-VL-Reranker-8B",
    )

    with pytest.raises(RuntimeError, match="incomplete rerank response"):
        adapter.score(
            "银行卡绑卡",
            [{"text": "银行卡绑定说明"}, {"text": "信用卡还款说明"}],
        )


def test_litellm_adapter_batches_all_candidates(monkeypatch):
    payloads = []
    responses = [
        {
            "results": [
                {"index": 0, "relevance_score": 0.1},
                {"index": 1, "relevance_score": 0.2},
            ]
        },
        {"results": [{"index": 0, "relevance_score": 0.3}]},
    ]

    def fake_post(url, *, headers, json, timeout):
        payloads.append(json)
        return _FakeResponse(responses[len(payloads) - 1])

    monkeypatch.setattr("openrag.retrieval.reranker.requests.post", fake_post)
    adapter = LiteLLMRerankAdapter(
        api_key="test-key",
        base_url="https://litellm.example/rerank",
        model="Qwen3-VL-Reranker-8B",
        max_documents=2,
    )

    scores = adapter.score(
        "银行卡绑卡",
        [{"text": "a"}, {"text": "b"}, {"text": "c"}],
    )

    assert scores == [0.1, 0.2, 0.3]
    assert [payload["documents"] for payload in payloads] == [["a", "b"], ["c"]]


def test_dashscope_adapter_sends_qwen3_vl_rerank_payload(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.91},
                        {"index": 0, "relevance_score": 0.23},
                    ]
                }
            }
        )

    monkeypatch.setattr("openrag.retrieval.reranker.requests.post", fake_post)

    adapter = DashScopeRerankAdapter(
        api_key="test-key",
        base_url="https://dashscope.example/rerank",
        model="qwen3-vl-rerank",
        timeout=12.0,
    )

    scores = adapter.score(
        "什么是重排序模型？",
        [
            {"text": "量子计算是计算机科学中的前沿领域。"},
            {"text": "重排序模型会根据查询和候选文档的相关性重新排序。"},
        ],
    )

    assert scores == [0.23, 0.91]
    assert captured["url"] == "https://dashscope.example/rerank"
    assert captured["timeout"] == 12.0
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"] == {
        "model": "qwen3-vl-rerank",
        "input": {
            "query": {"text": "什么是重排序模型？"},
            "documents": [
                {"text": "量子计算是计算机科学中的前沿领域。"},
                {"text": "重排序模型会根据查询和候选文档的相关性重新排序。"},
            ],
        },
        "parameters": {
            "return_documents": False,
            "top_n": 2,
        },
    }


def test_reranker_uses_dashscope_provider(monkeypatch):
    def fake_post(url, *, headers, json, timeout):
        return _FakeResponse(
            {
                "output": {
                    "results": [
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 1, "relevance_score": 0.95},
                    ]
                }
            }
        )

    monkeypatch.setenv("RERANKER_PROVIDER", "dashscope")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("RERANKER_MODEL", "qwen3-vl-rerank")
    monkeypatch.setenv("RERANKER_BASE_URL", "https://dashscope.example/rerank")
    monkeypatch.setattr("openrag.retrieval.reranker.requests.post", fake_post)

    reranker = Reranker(hierarchical_boost=0.0, position_boost=0.0)
    reranked = reranker.rerank(
        "什么是重排序模型？",
        [
            {"text": "量子计算是计算机科学中的前沿领域。", "score": 0.0, "file_id": 1},
            {"text": "重排序模型会根据查询和候选文档的相关性重新排序。", "score": 0.0, "file_id": 2},
        ],
        top_k=2,
    )

    assert [r["file_id"] for r in reranked] == [2, 1]
    assert reranked[0]["reranked_score"] > reranked[1]["reranked_score"]


def test_reranker_uses_litellm_provider(monkeypatch):
    captured = {}
    trace_service = Mock()

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(
            {
                "results": [
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 1, "relevance_score": 0.95},
                ]
            }
        )

    monkeypatch.setenv("RERANKER_PROVIDER", "litellm")
    monkeypatch.setenv("RERANKER_API_KEY", "test-key")
    monkeypatch.setenv("RERANKER_MODEL", "Qwen3-VL-Reranker-8B")
    monkeypatch.setenv("RERANKER_BASE_URL", "https://litellm.example/rerank")
    monkeypatch.setattr("openrag.retrieval.reranker.requests.post", fake_post)

    reranked = Reranker(hierarchical_boost=0.0, position_boost=0.0).rerank(
        "银行卡绑卡",
        [
            {"text": "信用卡还款说明", "score": 0.5, "file_id": 1},
            {"text": "银行卡绑定说明", "score": 0.5, "file_id": 2},
        ],
        top_k=2,
        trace_service=trace_service,
    )

    assert [result["file_id"] for result in reranked] == [2, 1]
    assert captured["url"] == "https://litellm.example/rerank"
    assert captured["json"]["model"] == "Qwen3-VL-Reranker-8B"
    assert trace_service.start_span.call_args.kwargs["input_summary"]["provider"] == "litellm"
    assert (
        trace_service.start_span.call_args.kwargs["input_summary"]["model"]
        == "Qwen3-VL-Reranker-8B"
    )


def test_litellm_failure_degrades_to_identity_without_local_model(monkeypatch):
    def failing_post(url, *, headers, json, timeout):
        raise RuntimeError("litellm unavailable")

    local_model_loader = Mock(return_value=None)
    trace_service = Mock()
    results = [
        {"chunk_id": "rrf-first", "text": "first", "score": 0.1, "fused_score": 0.02},
        {"chunk_id": "rrf-second", "text": "second", "score": 0.9, "fused_score": 0.01},
    ]
    monkeypatch.setenv("RERANKER_PROVIDER", "litellm")
    monkeypatch.setenv("RERANKER_API_KEY", "test-key")
    monkeypatch.setenv("RERANKER_MODEL", "Qwen3-VL-Reranker-8B")
    monkeypatch.setenv("RERANKER_BASE_URL", "https://litellm.example/rerank")
    monkeypatch.setattr("openrag.retrieval.reranker.requests.post", failing_post)
    monkeypatch.setattr(
        "openrag.retrieval.reranker._get_cross_encoder_model",
        local_model_loader,
    )

    reranked = Reranker().rerank(
        "银行卡绑卡", results, top_k=2, trace_service=trace_service
    )

    assert reranked == results
    assert all("reranked_score" not in result for result in reranked)
    local_model_loader.assert_not_called()
    assert trace_service.finish_span.call_args.kwargs["output_summary"] == {
        "result_count": 2,
        "applied": False,
        "degraded": True,
        "fallback": "identity",
        "reason": "litellm_request_failed",
    }
