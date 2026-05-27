"""Tests for DashScope API-based reranker adapter."""

from openrag.retrieval.reranker import DashScopeRerankAdapter, Reranker


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


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
