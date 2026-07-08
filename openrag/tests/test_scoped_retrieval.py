"""RetrievalService scope intersection + empty-scope early-return."""

import asyncio
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import openrag.api.search_api as sapi
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
    _bypass_active_file_filtering(svc)
    return svc


def _bypass_active_file_filtering(svc):
    svc._filter_to_active_file_ids = lambda file_ids: file_ids
    svc._filter_hits_to_active_files = lambda hits: hits


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
    _bypass_active_file_filtering(svc)
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
    _bypass_active_file_filtering(svc)
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
    _bypass_active_file_filtering(svc)
    svc.search(
        "q", user_id=1, workspace_id=7, top_k=5,
        scope_file_ids={2, 3}, vector_similarity_weight=0.7,
    )
    assert ft.calls and set(ft.calls[0]["file_ids"]) <= {2, 3}


def test_assert_search_workspace_read_blocks_without_permission(monkeypatch):
    monkeypatch.setattr(
        sapi.WorkspaceService, "check_user_permission",
        lambda self, ws, uid, perm: False,
    )
    with pytest.raises(HTTPException) as ei:
        sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=7)
    assert ei.value.status_code == 403


def test_assert_search_workspace_read_allows_with_permission(monkeypatch):
    monkeypatch.setattr(
        sapi.WorkspaceService, "check_user_permission",
        lambda self, ws, uid, perm: True,
    )
    sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=7)


def test_assert_search_workspace_read_skips_when_no_workspace():
    sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=None)


def test_semantic_search_blocks_before_execute(monkeypatch):
    """无 read 权限的 workspace 必须在进入 _execute_search（含 resolve/embedding）前 403。"""
    monkeypatch.setattr(
        sapi.WorkspaceService, "check_user_permission",
        lambda self, ws, uid, perm: False,
    )
    called = {"exec": False}
    monkeypatch.setattr(
        sapi, "_execute_search",
        lambda *a, **k: called.__setitem__("exec", True),
    )
    req = sapi.SearchRequest(query="q", workspace_id=7, paths=["/docs"])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(sapi.semantic_search(req, user_id=1, db=Mock()))
    assert ei.value.status_code == 403
    assert called["exec"] is False


# ---------------------------------------------------------------------------
# Task 6: empty scope must return before external deps; large scope L0 prefilter
# ---------------------------------------------------------------------------


class _FailingEmbeddingEngine:
    dimension = 3

    def embed_text(self, text):
        raise AssertionError("embedding should not run for empty scope")


class _FailingVectorStore:
    def search(self, *args, **kwargs):
        raise AssertionError("vector store should not run for empty scope")


class _FailingLayerStore:
    def search_layers(self, *args, **kwargs):
        raise AssertionError("layer store should not run for empty scope")


def test_execute_search_empty_scope_returns_before_external_dependencies(monkeypatch):
    monkeypatch.setattr(sapi, "resolve_scope_file_ids", lambda *a, **k: set())

    def _fail(*args, **kwargs):
        raise AssertionError(
            "external dependency should not be initialized for empty scope"
        )

    monkeypatch.setattr(sapi, "_prepare_retrieval_trace", _fail)
    monkeypatch.setattr(sapi, "_get_embedding_engine", _fail)
    monkeypatch.setattr(sapi, "_get_vector_store", _fail)
    monkeypatch.setattr(sapi, "_get_layer_store", _fail)
    monkeypatch.setattr(sapi, "_get_fulltext_store", _fail)

    resp = sapi._execute_search(
        Mock(),
        1,
        sapi.SearchRequest(query="q", workspace_id=7, paths=[]),
        endpoint="semantic",
        rerank_hierarchical_boost=None,
    )

    assert resp.results == []
    assert resp.total == 0


@pytest.mark.parametrize("use_contextual", [False, True])
def test_empty_scope_intersection_returns_before_embedding_and_store(use_contextual):
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FailingEmbeddingEngine(),
        vector_store=_FailingVectorStore(),
        layer_store=_FailingLayerStore() if use_contextual else None,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]

    results = svc.search(
        "q",
        user_id=1,
        workspace_id=7,
        top_k=5,
        use_contextual=use_contextual,
        retrieval_strategy="deep",
        scope_file_ids={99},
    )

    assert results == []


class _LargeScopeLayerStore:
    def __init__(self):
        self.calls = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        fid = int(file_ids[0]) if file_ids else -1
        return [{"file_id": fid, "score": 0.8, "layer_row_id": "r", "text": "t"}]


def test_contextual_large_scope_passes_file_ids_to_l0_layer_store():
    large_scope = set(range(1000, 1600))  # >512
    vs = _FakeVectorStore()
    ls = _LargeScopeLayerStore()
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs,
        layer_store=ls,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: list(range(900, 1700))
    svc._enrich_hits = lambda hits: None
    _bypass_active_file_filtering(svc)

    svc.search(
        "q",
        user_id=1,
        workspace_id=7,
        top_k=5,
        use_contextual=True,
        retrieval_strategy="deep",
        scope_file_ids=large_scope,
    )

    l0 = next(c for c in ls.calls if c["layer"] == "l0")
    assert l0["file_ids"] is not None
    assert set(l0["file_ids"]) == large_scope
