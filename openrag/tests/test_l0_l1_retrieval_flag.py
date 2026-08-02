from unittest.mock import Mock

from openrag.retrieval.retrieval_service import ResolvedFileScope, RetrievalService


class _FakeEmbeddingEngine:
    dimension = 3

    def embed_text(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self):
        self.calls = []

    def search(self, *, query_embedding, top_k, file_ids=None):
        self.calls.append(
            {
                "query_embedding": query_embedding,
                "top_k": top_k,
                "file_ids": file_ids,
            }
        )
        return [
            {
                "chunk_id": "chunk-1",
                "file_id": 1,
                "text": "plain L2 hit",
                "score": 0.9,
            }
        ]


class _FailingLayerStore:
    def search_layers(self, *args, **kwargs):
        raise AssertionError("L0/L1 layer search should be disabled")


def test_retrieval_service_disables_l0_l1_when_flag_false(monkeypatch):
    monkeypatch.setenv("OPENRAG_RETRIEVAL_USE_L0_L1", "false")
    vector_store = _FakeVectorStore()
    service = RetrievalService(
        db=Mock(),
        embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vector_store,
        layer_store=_FailingLayerStore(),
    )
    service._accessible_file_ids = lambda user_id, workspace_id=None: ResolvedFileScope.verified_global()
    service._enrich_hits = lambda hits: None
    service._filter_hits_to_active_files = lambda hits: hits
    service._validate_and_enrich_hits = lambda hits, **kwargs: (hits, {})

    results = service.search(
        "query",
        user_id=1,
        top_k=5,
        use_contextual=True,
        use_l1_llm_navigation=True,
    )

    assert results[0]["retrieval_mode"] == "flat"
    assert vector_store.calls[0]["top_k"] == 5
    assert results[0]["_l1_llm_applied"] is False
    assert results[0]["_l1_llm_skip_reason"] is None
