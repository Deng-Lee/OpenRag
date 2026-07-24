"""RetrievalService scope intersection + empty-scope early-return."""

import asyncio
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import openrag.api.search_api as sapi
from openrag.retrieval.retrieval_service import (
    PermissionScopeResolutionError,
    RetrievalService,
)


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


class _LeakyVectorStore:
    def __init__(self):
        self.calls = []

    def search(self, *, query_embedding, top_k, file_ids=None):
        self.calls.append({"top_k": top_k, "file_ids": file_ids})
        return [
            {"chunk_id": "allowed", "file_id": 2, "text": "allowed", "score": 0.9},
            {"chunk_id": "denied", "file_id": 99, "text": "denied", "score": 0.8},
            {"chunk_id": "malformed", "file_id": None, "text": "bad", "score": 0.7},
        ]


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


def test_permission_scope_resolution_error_is_fail_closed():
    db = Mock()
    db.query.return_value.filter.return_value.first.side_effect = RuntimeError(
        "sensitive database failure"
    )
    svc = RetrievalService.__new__(RetrievalService)
    svc.db = db

    with pytest.raises(PermissionScopeResolutionError):
        svc._accessible_file_ids(user_id=1)


def test_missing_user_has_empty_access_scope():
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = None
    svc = RetrievalService.__new__(RetrievalService)
    svc.db = db

    assert svc._accessible_file_ids(user_id=999, workspace_id=7) == []


@pytest.mark.parametrize("use_contextual", [False, True])
def test_permission_scope_failure_stops_before_embedding_and_stores(use_contextual):
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FailingEmbeddingEngine(),
        vector_store=_FailingVectorStore(),
        layer_store=_FailingLayerStore() if use_contextual else None,
    )
    svc._accessible_file_ids = Mock(
        side_effect=PermissionScopeResolutionError("permission scope unavailable")
    )

    with pytest.raises(PermissionScopeResolutionError):
        svc.search(
            "q",
            user_id=1,
            workspace_id=7,
            use_contextual=use_contextual,
            retrieval_strategy="deep",
        )


def test_flat_search_drops_unauthorized_hits_before_snapshot_and_enrichment():
    vs = _LeakyVectorStore()
    svc = _service(vs)
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [2]
    snapshotted = []
    enriched = []
    svc._record_chunk_search_snapshots = lambda hits: snapshotted.extend(hits)
    svc._enrich_hits = lambda hits: enriched.extend(hits)

    results = svc.search("q", user_id=1, workspace_id=7, top_k=5)

    assert [hit["file_id"] for hit in results] == [2]
    assert [hit["file_id"] for hit in snapshotted] == [2]
    assert [hit["file_id"] for hit in enriched] == [2]


class _FakeLayerStore:
    def __init__(self):
        self.calls = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        return [{"file_id": 2, "score": 0.8, "layer_row_id": "r", "text": "t"}]


class _LeakyLayerStore:
    def __init__(self):
        self.calls = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        return [
            {"file_id": 2, "score": 0.8, "layer_row_id": f"{layer}-2", "text": "ok"},
            {"file_id": 99, "score": 0.99, "layer_row_id": f"{layer}-99", "text": "no"},
        ]


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


def test_contextual_search_drops_unauthorized_l0_l1_l2_hits():
    vs = _LeakyVectorStore()
    ls = _LeakyLayerStore()
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs,
        layer_store=ls,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [2]
    svc._enrich_hits = lambda hits: None
    _bypass_active_file_filtering(svc)
    snapshotted = []
    svc._record_chunk_search_snapshots = lambda hits: snapshotted.extend(hits)

    results = svc.search(
        "q",
        user_id=1,
        workspace_id=7,
        top_k=5,
        use_contextual=True,
        retrieval_strategy="deep",
    )

    l1_call = next(call for call in ls.calls if call["layer"] == "l1")
    assert l1_call["file_ids"] == [2]
    assert vs.calls[0]["file_ids"] == [2]
    assert [hit["file_id"] for hit in snapshotted] == [2]
    assert [hit["file_id"] for hit in results] == [2]


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


def test_assert_search_workspace_read_wraps_resolution_failure(monkeypatch):
    def _fail(*args, **kwargs):
        raise RuntimeError("sensitive permission database failure")

    monkeypatch.setattr(
        sapi.WorkspaceService,
        "check_user_permission",
        _fail,
    )

    with pytest.raises(PermissionScopeResolutionError):
        sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=7)


def test_assert_search_workspace_read_skips_when_no_workspace():
    sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=None)


def test_semantic_search_blocks_before_external_dependencies(monkeypatch):
    """无 read 权限的 workspace 必须在进入 _execute_search（含 resolve/embedding）前 403。"""
    monkeypatch.setattr(
        sapi.WorkspaceService, "check_user_permission",
        lambda self, ws, uid, perm: False,
    )
    monkeypatch.setattr(
        sapi,
        "_prepare_retrieval_trace",
        lambda **kwargs: (Mock(), False),
    )

    def _fail(*args, **kwargs):
        raise AssertionError("retrieval dependency should not be initialized")

    monkeypatch.setattr(sapi, "_get_embedding_engine", _fail)
    monkeypatch.setattr(sapi, "_get_vector_store", _fail)
    monkeypatch.setattr(sapi, "_get_layer_store", _fail)
    monkeypatch.setattr(sapi, "_get_fulltext_store", _fail)
    req = sapi.SearchRequest(query="q", workspace_id=7, paths=["/docs"])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(sapi.semantic_search(req, user_id=1, db=Mock()))
    assert ei.value.status_code == 403


@pytest.mark.parametrize("handler", [sapi.semantic_search, sapi.hierarchical_search])
def test_jwt_search_permission_resolution_failure_returns_sanitized_503(
    monkeypatch, handler
):
    trace_service = Mock()
    monkeypatch.setattr(
        sapi,
        "_prepare_retrieval_trace",
        lambda **kwargs: (trace_service, True),
    )

    def _permission_fail(*args, **kwargs):
        raise RuntimeError("secret database connection details")

    monkeypatch.setattr(
        sapi.WorkspaceService,
        "check_user_permission",
        _permission_fail,
    )

    def _dependency_fail(*args, **kwargs):
        raise AssertionError("retrieval dependency should not be initialized")

    monkeypatch.setattr(sapi, "_get_embedding_engine", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_vector_store", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_layer_store", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_fulltext_store", _dependency_fail)

    request = sapi.SearchRequest(query="q", workspace_id=7)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(handler(request, user_id=1, db=Mock()))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Search authorization temporarily unavailable"
    assert "secret database" not in exc_info.value.detail
    trace_service.fail_run.assert_called_once_with(
        error_stage="authorization.scope_resolution",
        error_message="permission_scope_resolution_failed",
    )


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
    monkeypatch.setattr(
        sapi,
        "_prepare_retrieval_trace",
        lambda **kwargs: (Mock(), False),
    )
    monkeypatch.setattr(
        RetrievalService,
        "_accessible_file_ids",
        lambda self, user_id, workspace_id=None: [1, 2, 3],
    )

    def _fail(*args, **kwargs):
        raise AssertionError(
            "external dependency should not be initialized for empty scope"
        )

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


def test_path_scope_resolution_failure_returns_503_before_external_dependencies(
    monkeypatch,
):
    trace_service = Mock()
    monkeypatch.setattr(
        sapi,
        "_prepare_retrieval_trace",
        lambda **kwargs: (trace_service, True),
    )
    monkeypatch.setattr(
        sapi.WorkspaceService,
        "check_user_permission",
        lambda self, workspace_id, user_id, permission: True,
    )

    def _path_fail(*args, **kwargs):
        raise RuntimeError("secret path query SQL")

    monkeypatch.setattr(sapi, "resolve_scope_file_ids", _path_fail)

    def _dependency_fail(*args, **kwargs):
        raise AssertionError("retrieval dependency should not be initialized")

    monkeypatch.setattr(sapi, "_get_embedding_engine", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_vector_store", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_layer_store", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_fulltext_store", _dependency_fail)

    with pytest.raises(HTTPException) as exc_info:
        sapi._execute_search(
            Mock(),
            1,
            sapi.SearchRequest(query="q", workspace_id=7, paths=["/docs"]),
            endpoint="semantic",
            rerank_hierarchical_boost=None,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Search authorization temporarily unavailable"
    assert "secret path" not in exc_info.value.detail
    trace_service.fail_run.assert_called_once_with(
        error_stage="authorization.scope_resolution",
        error_message="permission_scope_resolution_failed",
    )


def test_invalid_path_scope_preserves_400(monkeypatch):
    monkeypatch.setattr(
        sapi,
        "_prepare_retrieval_trace",
        lambda **kwargs: (Mock(), False),
    )
    monkeypatch.setattr(
        sapi.WorkspaceService,
        "check_user_permission",
        lambda self, workspace_id, user_id, permission: True,
    )

    def _dependency_fail(*args, **kwargs):
        raise AssertionError("retrieval dependency should not be initialized")

    monkeypatch.setattr(sapi, "_get_embedding_engine", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_vector_store", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_layer_store", _dependency_fail)
    monkeypatch.setattr(sapi, "_get_fulltext_store", _dependency_fail)

    with pytest.raises(HTTPException) as exc_info:
        sapi._execute_search(
            Mock(),
            1,
            sapi.SearchRequest(query="q", workspace_id=7, paths=["../secret"]),
            endpoint="semantic",
            rerank_hierarchical_boost=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid path: path traversal detected"


def test_execute_search_maps_permission_scope_failure_to_sanitized_503(monkeypatch):
    trace_service = Mock()
    monkeypatch.setattr(
        sapi,
        "_prepare_retrieval_trace",
        lambda **kwargs: (trace_service, True),
    )
    monkeypatch.setattr(sapi, "_get_embedding_engine", lambda: Mock())
    monkeypatch.setattr(sapi, "_get_vector_store", lambda: Mock())
    monkeypatch.setattr(sapi, "_get_layer_store", lambda: None)
    monkeypatch.setattr(sapi, "_get_fulltext_store", lambda: None)
    runtime = Mock()
    runtime.snapshot = Mock(
        generation_id="generation-test",
        route_version=1,
        embedding_fingerprint="a" * 64,
        embedding_revision="revision-test",
        chunk_collection_name="chunks_test",
        layer_collection_name=None,
    )
    monkeypatch.setattr(sapi, "_resolve_search_runtime", lambda _db: runtime)

    root_cause = RuntimeError("secret database connection details")
    failure = PermissionScopeResolutionError("permission scope unavailable")
    failure.__cause__ = root_cause
    retrieval = Mock()
    retrieval.search.side_effect = failure
    monkeypatch.setattr(sapi, "RetrievalService", Mock(return_value=retrieval))

    with pytest.raises(HTTPException) as exc_info:
        sapi._execute_search(
            Mock(),
            1,
            sapi.SearchRequest(query="q"),
            endpoint="semantic",
            rerank_hierarchical_boost=None,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Search authorization temporarily unavailable"
    assert "secret database" not in exc_info.value.detail
    trace_service.fail_run.assert_called_once_with(
        error_stage="authorization.scope_resolution",
        error_message="permission_scope_resolution_failed",
    )


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


def test_contextual_fallback_reuses_single_permission_resolution():
    vs = _FakeVectorStore()
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs,
        layer_store=_EmptyL0LayerStore(),
    )
    svc._accessible_file_ids = Mock(return_value=[2])
    svc._enrich_hits = lambda hits: None
    _bypass_active_file_filtering(svc)

    svc.search(
        "q",
        user_id=1,
        workspace_id=7,
        use_contextual=True,
        retrieval_strategy="deep",
    )

    svc._accessible_file_ids.assert_called_once_with(1, 7)


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
