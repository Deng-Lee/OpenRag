import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401 - register all models
from openrag.api import search_api
from openrag.api.search_api import SearchRequest
from openrag.models import DocumentChunk, File, TraceRun, TraceSnapshot, TraceSpan, User, Workspace
from openrag.models.base import Base
from openrag.retrieval.retrieval_service import RetrievalService
from openrag.retrieval.retrieval_service import ResolvedFileScope
from openrag.retrieval.reranker import Reranker
from openrag.tracing.context import reset_trace_context


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        reset_trace_context()


class FakeEmbeddingEngine:
    model_name = "fake-embed-v1"
    dimension = 3

    def embed_text(self, query):
        assert "SECRET CHUNK BODY" not in query
        return [0.1, 0.2, 0.3]


class FakeVectorStore:
    def search(self, *, query_embedding, top_k, file_ids=None):
        return [
            {
                "chunk_id": "chunk-a",
                "file_id": 101,
                "score": 0.9,
                "text": "SECRET CHUNK BODY alpha",
            },
            {
                "chunk_id": "chunk-b",
                "file_id": 102,
                "score": 0.7,
                "text": "SECRET CHUNK BODY beta",
            },
            {
                "chunk_id": "chunk-c",
                "file_id": 103,
                "score": 0.2,
                "text": "SECRET CHUNK BODY gamma",
            },
        ][:top_k]


class FakeEsStore:
    def search_chunk_scores(self, *, index_names, query_text, file_ids, chunk_ids):
        assert index_names == ["idx-test"]
        return {"chunk-a": 0.1, "chunk-b": 4.0, "chunk-c": 0.0}


class IndependentFakeEsStore:
    def search_chunks(self, *, index_names, query_text, file_ids, top_k):
        assert index_names == ["idx-test"]
        return [
            {
                "chunk_id": "chunk-d",
                "file_id": 104,
                "text": "SECRET CHUNK BODY exact-code-A01",
                "sparse_score": 12.0,
            },
            {
                "chunk_id": "chunk-a",
                "file_id": 101,
                "text": "SECRET CHUNK BODY alpha",
                "sparse_score": 3.0,
            },
        ][:top_k]


def _seed_retrieval_rows(db_session):
    user = User(
        id=1,
        username="trace-user",
        email="trace@example.com",
        password_hash="x",
        full_name="Trace User",
    )
    workspace = Workspace(id=10, name="Trace WS", slug="trace-ws", owner_id=1)
    db_session.add_all([user, workspace])
    for offset, (file_id, chunk_id) in enumerate(
        [(101, "chunk-a"), (102, "chunk-b"), (103, "chunk-c"), (104, "chunk-d")]
    ):
        db_session.add(
            File(
                id=file_id,
                uri=f"/docs/{chunk_id}.txt",
                name=f"{chunk_id}.txt",
                owner_id=1,
                workspace_id=10,
                size=1,
            )
        )
        db_session.add(
            DocumentChunk(
                id=1000 + offset,
                file_id=file_id,
                workspace_id=10,
                chunk_id=chunk_id,
                chunk_index=0,
                object_key=f"chunks/{chunk_id}.md",
                text_preview=chunk_id,
            )
        )
    db_session.commit()


def _patch_search_dependencies(monkeypatch, fulltext_store, *, patch_index_resolver=True):
    monkeypatch.setattr(search_api, "_get_embedding_engine", lambda: FakeEmbeddingEngine())
    monkeypatch.setattr(search_api, "_get_vector_store", lambda: FakeVectorStore())
    monkeypatch.setattr(search_api, "_get_layer_store", lambda: None)
    monkeypatch.setattr(search_api, "_get_fulltext_store", lambda: fulltext_store)
    monkeypatch.setattr(
        search_api,
        "_resolve_search_runtime",
        lambda _db: SimpleNamespace(
            snapshot=SimpleNamespace(
                generation_id="generation-test",
                route_version=1,
                embedding_fingerprint="a" * 64,
                embedding_revision="revision-test",
                chunk_collection_name="chunks_test",
                layer_collection_name=None,
            ),
            embedding_engine=FakeEmbeddingEngine(),
            vector_store=FakeVectorStore(),
            layer_store=None,
        ),
    )
    monkeypatch.setattr(
        RetrievalService,
        "_accessible_file_ids",
        lambda self, user_id, workspace_id=None: ResolvedFileScope.finite([101, 102, 103, 104]),
    )
    if patch_index_resolver:
        monkeypatch.setattr(
            RetrievalService,
            "_resolve_es_index_names",
            lambda self, workspace_id, file_ids: ["idx-test"],
        )
    monkeypatch.setattr(
        RetrievalService,
        "_filter_hits_to_active_files",
        lambda self, hits: hits,
    )
    monkeypatch.setattr(
        Reranker,
        "_batch_cross_encoder_scores",
        lambda self, query, results: [0.95, 0.2, 0.1][: len(results)],
    )


def test_semantic_search_records_retrieval_trace_spans_and_top50_snapshots(
    db_session, monkeypatch
):
    _seed_retrieval_rows(db_session)
    monkeypatch.setenv("OPENRAG_RETRIEVAL_USE_L0_L1", "false")
    _patch_search_dependencies(monkeypatch, FakeEsStore())

    request = SearchRequest(
        query="foreign exchange policy",
        top_k=2,
        workspace_id=10,
        use_rerank=True,
        vector_similarity_weight=0.5,
        retrieval_strategy="flat",
    )

    response = search_api._execute_search(
        db_session,
        user_id=1,
        request=request,
        endpoint="semantic",
        rerank_hierarchical_boost=None,
        workspace_access_prevalidated=True,
    )

    assert response.total == 2
    run = db_session.query(TraceRun).one()
    assert run.trace_type == "retrieval"
    assert run.workspace_id == 10
    assert run.user_id == 1
    assert run.query_hash
    assert run.query_hash != request.query
    assert run.query_preview == request.query
    assert run.status == "success"
    assert run.search_config_snapshot["index_generation_id"] == "generation-test"
    assert run.search_config_snapshot["route_version"] == 1
    assert run.search_config_snapshot["embedding_fingerprint"] == "a" * 64
    assert run.search_config_snapshot["chunk_collection_name"] == "chunks_test"
    assert run.index_generation_id == "generation-test"
    assert run.route_version == 1
    assert run.embedding_fingerprint == "a" * 64
    assert run.collection_name == "chunks_test"

    spans = {
        span.stage: span
        for span in db_session.query(TraceSpan).order_by(TraceSpan.created_at, TraceSpan.id)
    }
    assert spans["retrieval.request"].input_summary == {
        "query_hash": run.query_hash,
        "query_preview": "foreign exchange policy",
        "workspace_id": 10,
        "top_k": 2,
        "use_rerank": True,
        "vector_similarity_weight": 0.5,
        "retrieval_strategy": "flat",
    }
    assert spans["retrieval.embed_query"].output_summary == {
        "embedding_model": "fake-embed-v1",
        "dimension": 3,
        "success": True,
    }
    assert spans["retrieval.response"].output_summary == {
        "final_result_count": 2,
        "zero_hit": False,
        "top50_source_file_count": 2,
            "fusion_overlap": 2,
            "rerank_overlap": 2,
            "sparse_rescued_final_count": 0,
        }
    assert not any("l0" in json.dumps(span.metrics or {}).lower() for span in spans.values())
    assert not any("l1" in json.dumps(span.metrics or {}).lower() for span in spans.values())

    snapshots = db_session.query(TraceSnapshot).order_by(TraceSnapshot.stage, TraceSnapshot.rank).all()
    serialized = json.dumps(
        [
            {
                "stage": item.stage,
                "rank": item.rank,
                "chunk_id": item.chunk_id,
                "file_id": item.file_id,
                "score": item.score,
                "score_parts": item.score_parts,
                "metadata": item.metadata_,
            }
            for item in snapshots
        ],
        ensure_ascii=False,
    )
    assert "SECRET CHUNK BODY" not in serialized

    chunk_search = [s for s in snapshots if s.stage == "retrieval.chunk_search"]
    assert [(s.rank, s.chunk_id, s.file_id, s.score_parts["vector_score"]) for s in chunk_search] == [
        (1, "chunk-a", 101, 0.9),
        (2, "chunk-b", 102, 0.7),
        (3, "chunk-c", 103, 0.2),
    ]

    fusion_inputs = [
        s
        for s in snapshots
        if s.stage == "retrieval.es_fusion" and s.metadata_["phase"] == "input"
    ]
    fusion_outputs = [
        s
        for s in snapshots
        if s.stage == "retrieval.es_fusion" and s.metadata_["phase"] == "output"
    ]
    assert len(fusion_inputs) == 3
    assert len(fusion_outputs) == 3
    assert fusion_outputs[0].chunk_id == "chunk-b"
    assert set(fusion_outputs[0].score_parts) == {
        "vector_score",
        "bm25_score",
        "fused_score",
    }

    rerank_inputs = [
        s
        for s in snapshots
        if s.stage == "retrieval.rerank" and s.metadata_["phase"] == "input"
    ]
    rerank_outputs = [
        s
        for s in snapshots
        if s.stage == "retrieval.rerank" and s.metadata_["phase"] == "output"
    ]
    assert len(rerank_inputs) == 3
    assert len(rerank_outputs) == 2
    assert rerank_outputs[0].chunk_id == "chunk-b"
    assert rerank_outputs[0].metadata_["original_rank"] == 1
    assert rerank_outputs[0].metadata_["rank_delta"] == 0
    assert set(rerank_outputs[0].score_parts) == {
        "fused_score",
        "retrieval_score",
        "retrieval_normalized_score",
        "rerank_model_score",
        "base_score",
        "hierarchy_boost",
        "position_boost",
        "rerank_score",
    }
    assert spans["retrieval.rerank"].output_summary["applied"] is True
    assert spans["retrieval.rerank"].output_summary["degraded"] is False


def test_elasticsearch_failure_records_skip_reason_without_breaking_search(
    db_session, monkeypatch
):
    class BrokenEsStore:
        def search_chunks(self, **kwargs):
            raise RuntimeError("es unavailable")

    _seed_retrieval_rows(db_session)
    monkeypatch.setenv("OPENRAG_RETRIEVAL_USE_L0_L1", "false")
    monkeypatch.setattr(
        "openrag.config.get_config",
        lambda: SimpleNamespace(
            elasticsearch=SimpleNamespace(hybrid_recall_mode="independent_rrf")
        ),
    )
    _patch_search_dependencies(monkeypatch, BrokenEsStore())

    request = SearchRequest(
        query="policy",
        top_k=2,
        workspace_id=10,
        use_rerank=False,
        vector_similarity_weight=0.5,
        retrieval_strategy="flat",
    )

    response = search_api._execute_search(
        db_session,
        user_id=1,
        request=request,
        endpoint="semantic",
        rerank_hierarchical_boost=None,
        workspace_access_prevalidated=True,
    )

    assert response.total == 2
    assert [result.score for result in response.results] == [0.9, 0.7]
    run = db_session.query(TraceRun).one()
    assert run.status == "success"
    es_span = db_session.query(TraceSpan).filter_by(stage="retrieval.sparse_recall").one()
    assert es_span.status == "success"
    assert es_span.output_summary["skip_reason"] == "sparse_error"
    assert es_span.output_summary["degraded"] is True
    assert "es unavailable" in es_span.output_summary["error"]


def test_flat_hybrid_returns_sparse_only_hit(db_session, monkeypatch):
    """A-01 baseline: independent Sparse must expand the Dense candidate set."""
    _seed_retrieval_rows(db_session)
    monkeypatch.setenv("OPENRAG_RETRIEVAL_USE_L0_L1", "false")
    monkeypatch.setattr(
        "openrag.config.get_config",
        lambda: SimpleNamespace(
            elasticsearch=SimpleNamespace(hybrid_recall_mode="independent_rrf")
        ),
    )
    _patch_search_dependencies(monkeypatch, IndependentFakeEsStore())

    request = SearchRequest(
        query="exact-code-A01",
        top_k=4,
        workspace_id=10,
        use_rerank=False,
        vector_similarity_weight=0.5,
        retrieval_strategy="flat",
    )
    response = search_api._execute_search(
        db_session,
        user_id=1,
        request=request,
        endpoint="semantic",
        rerank_hierarchical_boost=None,
        workspace_access_prevalidated=True,
    )

    assert "chunk-d" in {result.chunk_id for result in response.results}


def test_flat_hybrid_union_is_reranked_before_final_top_k(db_session, monkeypatch):
    _seed_retrieval_rows(db_session)
    monkeypatch.setenv("OPENRAG_RETRIEVAL_USE_L0_L1", "false")
    monkeypatch.setattr(
        "openrag.config.get_config",
        lambda: SimpleNamespace(
            elasticsearch=SimpleNamespace(hybrid_recall_mode="independent_rrf")
        ),
    )
    _patch_search_dependencies(monkeypatch, IndependentFakeEsStore())
    captured = {}

    def capture_rerank(self, query, results, top_k, trace_service=None):
        captured["chunk_ids"] = [result["chunk_id"] for result in results]
        return [
            {**result, "reranked_score": 1.0 - index * 0.1}
            for index, result in enumerate(results[:top_k])
        ]

    monkeypatch.setattr(Reranker, "rerank", capture_rerank)
    request = SearchRequest(
        query="exact-code-A01",
        top_k=2,
        workspace_id=10,
        use_rerank=True,
        vector_similarity_weight=0.5,
        retrieval_strategy="flat",
    )

    response = search_api._execute_search(
        db_session,
        user_id=1,
        request=request,
        endpoint="semantic",
        rerank_hierarchical_boost=None,
        workspace_access_prevalidated=True,
    )

    assert set(captured["chunk_ids"]) == {"chunk-a", "chunk-b", "chunk-c", "chunk-d"}
    assert response.total == 2
    assert response.results[0].score == 1.0
    spans = {span.stage: span for span in db_session.query(TraceSpan).all()}
    assert spans["retrieval.dense_recall"].output_summary["hit_count"] == 3
    assert spans["retrieval.sparse_recall"].output_summary["hit_count"] == 2
    assert spans["retrieval.hybrid_fusion"].output_summary == {
        "dense_count": 3,
        "sparse_count": 2,
        "overlap_count": 1,
        "dense_only_count": 2,
        "sparse_only_count": 1,
        "union_count": 4,
    }
    assert spans["retrieval.db_validation"].output_summary["valid_hit_count"] == 4
    fusion_snapshots = db_session.query(TraceSnapshot).filter_by(
        stage="retrieval.hybrid_fusion"
    ).all()
    assert len(fusion_snapshots) == 4
    assert any(snapshot.metadata_["recall_sources"] == ["sparse"] for snapshot in fusion_snapshots)


def test_v2_sparse_trace_records_contract_and_exact_match_channel(
    db_session, monkeypatch
):
    class V2EsStore:
        def resolve_read_target(
            self, *, legacy_index, read_alias, chunk_index_mode
        ):
            assert legacy_index == "openrag_ws_trace-ws_chunks"
            assert read_alias == "openrag_ws_trace-ws_chunks_read"
            assert chunk_index_mode == "v2_alias"
            return read_alias

        def get_alias_state(self, aliases):
            return {
                aliases[0]: {
                    "openrag_ws_trace-ws_chunks_v2": {},
                }
            }

        def search_chunks(self, **kwargs):
            assert kwargs["chunk_index_mode"] == "v2_alias"
            assert kwargs["index_names"] == ["openrag_ws_trace-ws_chunks_read"]
            return [
                {
                    "chunk_id": "chunk-d",
                    "file_id": 104,
                    "text": "SECRET CHUNK BODY exact",
                    "sparse_score": 12.0,
                    "matched_queries": ["content_bm25", "exact_identifier"],
                    "exact_term_match": True,
                }
            ]

    _seed_retrieval_rows(db_session)
    monkeypatch.setenv("OPENRAG_RETRIEVAL_USE_L0_L1", "false")
    monkeypatch.setattr(
        "openrag.config.get_config",
        lambda: SimpleNamespace(
            elasticsearch=SimpleNamespace(
                hybrid_recall_mode="independent_rrf",
                chunk_index_mode="v2_alias",
            )
        ),
    )
    _patch_search_dependencies(
        monkeypatch, V2EsStore(), patch_index_resolver=False
    )

    request = SearchRequest(
        query="ERR/A02-503.1",
        top_k=4,
        workspace_id=10,
        use_rerank=False,
        vector_similarity_weight=0.5,
        retrieval_strategy="flat",
    )
    response = search_api._execute_search(
        db_session,
        user_id=1,
        request=request,
        endpoint="semantic",
        rerank_hierarchical_boost=None,
        workspace_access_prevalidated=True,
    )

    assert "chunk-d" in {result.chunk_id for result in response.results}
    span = db_session.query(TraceSpan).filter_by(
        stage="retrieval.sparse_recall"
    ).one()
    assert span.output_summary["chunk_index_mode"] == "v2_alias"
    assert span.output_summary["physical_index_names"] == [
        "openrag_ws_trace-ws_chunks_v2"
    ]
    assert span.output_summary["schema_version"] == "a02-content-exact-v2"
    assert len(span.output_summary["mapping_hash"]) == 64
    assert span.output_summary["query_profile_version"] == (
        "a02-content-exact-v1"
    )
    assert span.output_summary["query_exact_term_count"] == 1
    snapshot = db_session.query(TraceSnapshot).filter_by(
        stage="retrieval.sparse_recall"
    ).one()
    assert snapshot.metadata_["matched_queries"] == [
        "content_bm25",
        "exact_identifier",
    ]
    assert snapshot.metadata_["exact_term_match"] is True
