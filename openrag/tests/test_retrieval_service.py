from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from openrag.models import Base, DocumentChunk, File, TraceSpan, User, Workspace
from openrag.retrieval.retrieval_service import ResolvedFileScope, RetrievalService
from openrag.tracing.context import reset_trace_context, set_trace_context


class FakeEmbeddingEngine:
    model_name = "fake"

    def embed_text(self, query):
        return [0.1, 0.2]


class FakeVectorStore:
    def __init__(self, hits):
        self.hits = hits

    def search(self, *, query_embedding, top_k, file_ids=None):
        return [dict(hit) for hit in self.hits[:top_k]]


@pytest.fixture
def retrieval_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="retrieval-user",
        email="retrieval@example.com",
        password_hash="x",
        full_name="Retrieval User",
    )
    db.add(user)
    db.commit()
    ws = Workspace(name="WS", slug="ws", owner_id=user.id)
    other_ws = Workspace(name="Other", slug="other", owner_id=user.id)
    db.add_all([ws, other_ws])
    db.commit()
    files = {
        "allowed": File(uri="/allowed", name="allowed", owner_id=user.id, workspace_id=ws.id),
        "forbidden": File(uri="/forbidden", name="forbidden", owner_id=user.id, workspace_id=ws.id),
        "deleted": File(
            uri="/deleted",
            name="deleted",
            owner_id=user.id,
            workspace_id=ws.id,
            deleted_at=datetime.now(timezone.utc),
        ),
        "other": File(uri="/other", name="other", owner_id=user.id, workspace_id=other_ws.id),
    }
    db.add_all(files.values())
    db.commit()
    for index, (chunk_id, file_key, workspace_id) in enumerate(
        [
            ("valid", "allowed", ws.id),
            ("forged", "forbidden", ws.id),
            ("deleted", "deleted", ws.id),
            ("wrong-workspace", "other", other_ws.id),
            ("unauthorized", "forbidden", ws.id),
        ],
        start=1,
    ):
        db.add(
            DocumentChunk(
                id=index,
                file_id=files[file_key].id,
                workspace_id=workspace_id,
                chunk_id=chunk_id,
                chunk_index=index,
                object_key=f"chunks/{chunk_id}.md",
                text_preview=f"preview-{chunk_id}",
                page=index,
            )
        )
    db.commit()
    try:
        yield engine, db, user, ws, files
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        reset_trace_context()


def test_search_validates_hits_against_authoritative_db_and_records_counts(retrieval_db):
    _engine, db, user, ws, files = retrieval_db
    hits = [
        {"chunk_id": "valid", "file_id": files["allowed"].id, "text": "valid", "score": 0.9},
        {"chunk_id": "orphan", "file_id": files["allowed"].id, "text": "orphan", "score": 0.8},
        {"chunk_id": "forged", "file_id": files["allowed"].id, "text": "forged", "score": 0.7},
        {"chunk_id": "deleted", "file_id": files["deleted"].id, "text": "deleted", "score": 0.6},
        {"chunk_id": "wrong-workspace", "file_id": files["other"].id, "text": "other", "score": 0.5},
        {"chunk_id": "unauthorized", "file_id": files["forbidden"].id, "text": "forbidden", "score": 0.4},
    ]
    service = RetrievalService(db, FakeEmbeddingEngine(), FakeVectorStore(hits))
    service._accessible_file_ids = lambda *args, **kwargs: ResolvedFileScope.finite(
        [files["allowed"].id, files["deleted"].id, files["other"].id]
    )
    set_trace_context(trace_id="db-validation", trace_type="retrieval")
    service.trace_service.start_run(trace_id="db-validation", trace_type="retrieval")

    results = service.search(
        "q", user_id=user.id, workspace_id=ws.id, top_k=10, vector_similarity_weight=1.0
    )

    assert [result["chunk_id"] for result in results] == ["valid"]
    assert results[0]["filename"] == "allowed"
    assert results[0]["text_preview"] == "preview-valid"
    assert results[0]["page"] == 1
    span = db.query(TraceSpan).filter_by(
        trace_id="db-validation", stage="retrieval.db_validation"
    ).one()
    assert span.output_summary["stale_hit_count"] == 2
    assert span.output_summary["unauthorized_hit_count"] == 1
    assert span.output_summary["ownership_mismatch_count"] == 2


def test_validation_uses_constant_number_of_chunk_and_file_queries(retrieval_db):
    engine, db, user, ws, files = retrieval_db
    allowed = files["allowed"]
    for index in range(10, 30):
        db.add(
            DocumentChunk(
                id=index,
                file_id=allowed.id,
                workspace_id=ws.id,
                chunk_id=f"batch-{index}",
                chunk_index=index,
                object_key=f"chunks/batch-{index}.md",
            )
        )
    db.commit()
    hits = [
        {"chunk_id": f"batch-{index}", "file_id": allowed.id, "text": "x", "score": 1.0}
        for index in range(10, 30)
    ]
    service = RetrievalService(db, FakeEmbeddingEngine(), FakeVectorStore(hits))
    service._accessible_file_ids = lambda *args, **kwargs: ResolvedFileScope.finite([allowed.id])
    selects = {"document_chunks": 0, "files": 0}

    def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        lowered = statement.lower()
        if lowered.lstrip().startswith("select"):
            if "from document_chunks" in lowered:
                selects["document_chunks"] += 1
            elif "from files" in lowered:
                selects["files"] += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        results = service.search("q", user_id=user.id, workspace_id=ws.id, top_k=20)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert len(results) == 20
    assert selects == {"document_chunks": 1, "files": 1}
