import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401 - register all models
from openrag.api import search_api
from openrag.api.search_api import SearchRequest
from openrag.models import TraceRun, TraceSnapshot, TraceSpan
from openrag.models.base import Base
from openrag.retrieval.retrieval_service import RetrievalService
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


def _patch_search_dependencies(monkeypatch, fulltext_store):
    monkeypatch.setattr(search_api, "_get_embedding_engine", lambda: FakeEmbeddingEngine())
    monkeypatch.setattr(search_api, "_get_vector_store", lambda: FakeVectorStore())
    monkeypatch.setattr(search_api, "_get_layer_store", lambda: None)
    monkeypatch.setattr(search_api, "_get_fulltext_store", lambda: fulltext_store)
    monkeypatch.setattr(
        RetrievalService,
        "_accessible_file_ids",
        lambda self, user_id, workspace_id=None: [101, 102, 103],
    )
    monkeypatch.setattr(
        RetrievalService,
        "_resolve_es_index_names",
        lambda self, workspace_id, file_ids: ["idx-test"],
    )
    monkeypatch.setattr(
        Reranker,
        "_batch_cross_encoder_scores",
        lambda self, query, results: [0.95, 0.2, 0.1][: len(results)],
    )


def test_semantic_search_records_retrieval_trace_spans_and_top50_snapshots(
    db_session, monkeypatch
):
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
        "rerank_score",
    }


def test_elasticsearch_failure_records_skip_reason_without_breaking_search(
    db_session, monkeypatch
):
    class BrokenEsStore:
        def search_chunk_scores(self, **kwargs):
            raise RuntimeError("es unavailable")

    monkeypatch.setenv("OPENRAG_RETRIEVAL_USE_L0_L1", "false")
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
    run = db_session.query(TraceRun).one()
    assert run.status == "success"
    es_span = db_session.query(TraceSpan).filter_by(stage="retrieval.es_fusion").one()
    assert es_span.status == "success"
    assert es_span.output_summary["skip_reason"] == "error"
    assert "es unavailable" in es_span.output_summary["error"]
