"""RetrievalService scope intersection + empty-scope early-return."""

from unittest.mock import Mock

from openrag.retrieval.retrieval_service import RetrievalService


class _FakeEmbeddingEngine:
    dimension = 3

    def embed_text(self, text):
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self):
        self.calls = []

    def search(self, *, query_embedding, top_k, file_ids=None):
        self.calls.append({"top_k": top_k, "file_ids": file_ids})
        return [{"chunk_id": "c1", "file_id": 2, "text": "hit", "score": 0.9}]


def _service(vector_store):
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vector_store,
        layer_store=None,
    )
    svc._enrich_hits = lambda hits: None
    return svc


def test_effective_file_ids_intersects_and_handles_none():
    svc = _service(_FakeVectorStore())
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    assert sorted(svc._effective_file_ids(1, 7, {2, 3, 9})) == [2, 3]
    assert svc._effective_file_ids(1, 7, None) == [1, 2, 3]
    assert svc._effective_file_ids(1, 7, set()) == []
    svc._accessible_file_ids = lambda user_id, workspace_id=None: None
    assert sorted(svc._effective_file_ids(1, None, {5, 6})) == [5, 6]
    assert svc._effective_file_ids(1, None, None) is None


def test_search_passes_scope_intersection_to_vector_store():
    vs = _FakeVectorStore()
    svc = _service(vs)
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc.search("q", user_id=1, workspace_id=7, top_k=5, scope_file_ids={2, 3, 99})
    assert sorted(vs.calls[0]["file_ids"]) == [2, 3]


def test_empty_scope_returns_empty_without_calling_store():
    vs = _FakeVectorStore()
    svc = _service(vs)
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    results = svc.search("q", user_id=1, workspace_id=7, top_k=5, scope_file_ids=set())
    assert results == []
    assert vs.calls == []


def test_no_scope_keeps_existing_behavior():
    vs = _FakeVectorStore()
    svc = _service(vs)
    svc._accessible_file_ids = lambda user_id, workspace_id=None: None
    svc.search("q", user_id=1, top_k=5)
    assert vs.calls[0]["file_ids"] is None


class _FakeLayerStore:
    def __init__(self):
        self.calls = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        return [{"file_id": 2, "score": 0.8, "layer_row_id": "r", "text": "t"}]


class _EmptyL0LayerStore:
    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        return []


class _FakeFulltext:
    def __init__(self):
        self.calls = []

    def search_chunk_scores(self, *, index_names, query_text, file_ids, chunk_ids):
        self.calls.append({"file_ids": file_ids})
        return {}


def test_contextual_scope_limits_l0_l1_l2():
    vs = _FakeVectorStore()
    ls = _FakeLayerStore()
    svc = RetrievalService(
        db=Mock(), embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs, layer_store=ls,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc._enrich_hits = lambda hits: None
    svc.search(
        "q", user_id=1, workspace_id=7, top_k=5,
        use_contextual=True, retrieval_strategy="deep", scope_file_ids={2},
    )
    l0 = next(c for c in ls.calls if c["layer"] == "l0")
    assert l0["file_ids"] == [2]
    l1 = next(c for c in ls.calls if c["layer"] == "l1")
    assert l1["file_ids"] == [2]
    assert vs.calls[0]["file_ids"] == [2]


def test_contextual_fallback_to_flat_carries_scope():
    vs = _FakeVectorStore()
    svc = RetrievalService(
        db=Mock(), embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs, layer_store=_EmptyL0LayerStore(),
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc._enrich_hits = lambda hits: None
    svc.search(
        "q", user_id=1, workspace_id=7, top_k=5,
        use_contextual=True, retrieval_strategy="deep", scope_file_ids={2},
    )
    assert set(vs.calls[0]["file_ids"]) <= {2}


def test_es_blend_filter_ids_within_scope():
    vs = _FakeVectorStore()
    ft = _FakeFulltext()
    svc = RetrievalService(
        db=Mock(), embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs, layer_store=None, fulltext_store=ft,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc._enrich_hits = lambda hits: None
    svc._resolve_es_index_names = lambda workspace_id, file_ids: ["idx"]
    svc.search(
        "q", user_id=1, workspace_id=7, top_k=5,
        scope_file_ids={2, 3}, vector_similarity_weight=0.7,
    )
    assert ft.calls and set(ft.calls[0]["file_ids"]) <= {2, 3}
