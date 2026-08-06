import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401 - register models
from openrag.chunking.chunk_models import Chunk
from openrag.models import (
    DocumentChunk,
    DocumentParseArtifact,
    File,
    TraceRun,
    TraceSpan,
)
from openrag.models.base import Base
from openrag.models.file import ProcessingStatus
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.indexing.runtime import IndexRuntime
from openrag.parsers.base import DocumentBlock
from openrag.processors.document_processor import DocumentProcessor, FulltextIndexingError
from openrag.processors.document_processor import (
    _coerce_int_list,
    _coerce_position_int,
    _extract_chunk_position_fields,
)
from openrag.services import file_ingest
from openrag.tracing.context import reset_trace_context, set_trace_context
from openrag.worker import task_worker


class FakeUploadMinio:
    def __init__(self):
        self.puts = []

    def put_file(
        self, bucket_name, object_name, data, content_type="application/octet-stream"
    ):
        self.puts.append((bucket_name, object_name, data, content_type))


class FakeProcessingMinio:
    def __init__(self):
        self.objects = {}

    def get_file_to_path(self, bucket_name, object_name, file_path):
        Path(file_path).write_bytes(b"source document bytes")

    def put_file(
        self, bucket_name, object_name, data, content_type="application/octet-stream"
    ):
        self.objects[(bucket_name, object_name)] = {
            "data": data,
            "content_type": content_type,
        }

    def put_document_hierarchy(self, bucket_name, file_uri, l0, l1, l2):
        self.put_file(
            bucket_name,
            f"hierarchy/{file_uri.lstrip('/')}.abstract.md",
            l0.encode("utf-8"),
            "text/markdown",
        )
        self.put_file(
            bucket_name,
            f"hierarchy/{file_uri.lstrip('/')}.overview.md",
            l1.encode("utf-8"),
            "text/markdown",
        )
        for i, chunk in enumerate(l2):
            self.put_file(
                bucket_name,
                f"hierarchy/{file_uri.lstrip('/')}/chunks/{i:04d}.md",
                chunk.text.encode("utf-8"),
                "text/markdown",
            )
        return {
            "l0_url": "minio://l0",
            "l1_url": "minio://l1",
            "l2_url": "minio://chunks/",
        }

    def path_style_http_url(self, bucket_name, object_key):
        return f"http://minio/{bucket_name}/{object_key}"


class FakeParser:
    parser_version = "2026.05"

    def parse(self, file_path):
        assert Path(file_path).read_bytes() == b"source document bytes"
        return [
            DocumentBlock(
                text="Heading",
                page=1,
                offset=0,
                block_type="heading",
                level=1,
                block_id="b1",
                char_start=0,
                char_end=7,
            ),
            DocumentBlock(
                text="SECRET FULL PARSE BODY",
                page=2,
                offset=8,
                block_type="text",
                block_id="b2",
                char_start=8,
                char_end=30,
            ),
        ]


class FakeParserRegistry:
    def get_parser(self, file_path, parser_type):
        return FakeParser()


class FakeChunkEngine:
    def chunk(
        self,
        text_blocks,
        chunk_size,
        chunk_overlap,
        chunk_method=None,
        min_chunk_tokens=0,
    ):
        return [
            Chunk(
                text="Heading body GB/T 35273-2020",
                chunk_id="chunk-1",
                page=1,
                level=1,
                block_type="heading",
                metadata={
                    "page_num_int": [1, 1],
                    "position_int": [
                        [1, 10, 100, 20, 40],
                        [1, 10, 100, 45, 65],
                    ],
                    "top_int": [20, 45],
                    "content_ltks": "forbidden old token",
                    "content_sm_ltks": "forbidden old token",
                    "mom_with_weight": "parent compatibility text",
                },
            ),
            Chunk(text="tiny", chunk_id="chunk-2", page=2, block_type="text"),
            Chunk(text="small", chunk_id="chunk-3", page=2, block_type="text"),
        ]


class EmptyChunkEngine:
    def chunk(self, text_blocks, chunk_size, chunk_overlap, chunk_method=None, min_chunk_tokens=0):
        return []


class FakeEmbeddingEngine:
    model_name = "fake-embedding"
    dimension = 3

    def assert_fingerprint(self, _fingerprint):
        return None

    def embed_chunks(self, chunks):
        return [(chunk, [0.1, 0.2, 0.3]) for chunk in chunks]

    def embed_text(self, text):
        return [0.1, 0.2, 0.3]


class FakeVectorStore:
    collection_name = "fake_chunks"

    def delete_by_file_id(self, file_id):
        self.deleted_file_id = file_id

    def insert_chunks(self, file_id, chunk_embeddings, workspace_id=None):
        return len(chunk_embeddings)


class FakeEsStore:
    def __init__(self, *, upsert_count=None):
        self.operations = []
        self.upsert_count = upsert_count

    def ensure_index(self, index_name):
        self.index_name = index_name

    def delete_by_file_id(self, index_name, file_id, *, required=False):
        self.operations.append(("delete", index_name, file_id))

    def bulk_upsert_chunks(self, index_name, docs):
        self.operations.append(("bulk", index_name, [doc["chunk_id"] for doc in docs]))
        self.docs = docs
        return len(docs) if self.upsert_count is None else self.upsert_count


class FakeV2EsStore(FakeEsStore):
    def resolve_write_target(
        self, *, legacy_index, write_alias, chunk_index_mode
    ):
        assert chunk_index_mode == "v2_alias"
        return write_alias

    def get_alias_state(self, aliases):
        return {
            alias: {"workspace-chunks-v2": {"is_write_index": True}}
            for alias in aliases
        }


class TxtParserAdapter:
    parser_version = "canonical-test"

    def parse(self, file_path):
        assert Path(file_path).read_bytes() == b"source text"
        return [
            DocumentBlock(
                text="  Alpha  ",
                page=1,
                offset=10,
                block_type="text",
                block_id="txt:p:0",
                char_start=100,
                char_end=109,
            ),
            DocumentBlock(
                text="\nBeta\n",
                page=1,
                offset=20,
                block_type="text",
                block_id="txt:p:1",
                char_start=200,
                char_end=206,
            ),
        ]


class FakeCanonicalParserRegistry:
    def get_parser(self, file_path, parser_type):
        return TxtParserAdapter()


class FakeCanonicalChunkEngine:
    def __init__(self):
        self.seen_blocks = None

    def chunk(
        self,
        text_blocks,
        chunk_size,
        chunk_overlap,
        chunk_method=None,
        min_chunk_tokens=0,
    ):
        self.seen_blocks = list(text_blocks)
        assert [block.text for block in self.seen_blocks] == ["Alpha", "Beta"]
        assert [(block.char_start, block.char_end) for block in self.seen_blocks] == [
            (0, 5),
            (6, 10),
        ]
        return [
            Chunk(
                text="Beta",
                chunk_id="canonical-chunk-1",
                page=1,
                start_offset=6,
                end_offset=10,
                block_type="text",
                source_block_id="txt:p:1",
                source_char_start=6,
                source_char_end=10,
            )
        ]


def _new_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _seed_file(db):
    user = User(
        username="u1",
        email="u1@example.com",
        password_hash="x",
        full_name="User One",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    workspace = Workspace(name="WS", slug="ws", owner_id=user.id)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    file = File(
        uri="/docs/report.pdf",
        name="report.pdf",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=128,
        mime_type="application/pdf",
    )
    db.add(file)
    db.commit()
    db.refresh(file)
    return user, workspace, file


def teardown_function():
    reset_trace_context()


def test_extract_chunk_position_fields_coerces_ragflow_metadata():
    chunk = Chunk(
        text="positioned",
        chunk_id="chunk-positioned",
        metadata={
            "page_num_int": ("1",),
            "position_int": [(1, "10", 120, 30.0, 58)],
            "top_int": ["30"],
        },
    )

    assert _coerce_int_list(chunk.metadata["page_num_int"]) == [1]
    assert _coerce_position_int(chunk.metadata["position_int"]) == [
        [1, 10, 120, 30, 58]
    ]
    assert _extract_chunk_position_fields(chunk) == {
        "page_num_int": [1],
        "position_int": [[1, 10, 120, 30, 58]],
        "top_int": [30],
    }


def test_document_processor_uses_canonical_source_for_text_like_parsers(tmp_path):
    engine, db = _new_db()
    try:
        user, workspace, file = _seed_file(db)
        file.uri = "/docs/notes.txt"
        file.name = "notes.txt"
        file.mime_type = "text/plain"
        db.commit()
        source_path = tmp_path / "notes.txt"
        source_path.write_bytes(b"source text")
        processing_minio = FakeProcessingMinio()
        chunk_engine = FakeCanonicalChunkEngine()

        processor = DocumentProcessor(
            db=db,
            parser_registry=FakeCanonicalParserRegistry(),
            chunk_engine=chunk_engine,
            embedding_engine=FakeEmbeddingEngine(),
            fulltext_required=False,
            minio_storage=processing_minio,
            vector_store=FakeVectorStore(),
            layer_store=None,
            chunk_fulltext_store=None,
        )

        result = processor.process_document(
            file_path=str(source_path),
            file_id=file.id,
            user_id=user.id,
            parser_type="txt",
        )

        assert result["status"] == "completed"
        assert [block.text for block in chunk_engine.seen_blocks] == ["Alpha", "Beta"]
        artifact = db.query(DocumentParseArtifact).one()
        md_object = processing_minio.objects[
            (workspace.slug, artifact.canonical_md_object_key)
        ]
        json_object = processing_minio.objects[
            (workspace.slug, artifact.canonical_json_object_key)
        ]
        payload = json.loads(json_object["data"].decode("utf-8"))
        stored_chunk = (
            db.query(DocumentChunk).filter_by(chunk_id="canonical-chunk-1").one()
        )

        assert md_object["data"].decode("utf-8") == "Alpha\nBeta"
        assert payload["canonical_source"]["version"] == "chunk_source_v1"
        assert payload["blocks"][1]["char_start"] == 6
        assert payload["blocks"][1]["char_end"] == 10
        assert stored_chunk.source_block_id == "txt:p:1"
        assert stored_chunk.source_char_start == 6
        assert stored_chunk.source_char_end == 10
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_required_document_processing_rejects_unavailable_fulltext_store(tmp_path):
    engine, db = _new_db()
    try:
        _user, _workspace, file = _seed_file(db)

        with pytest.raises(FulltextIndexingError, match="unavailable"):
            DocumentProcessor(
                db=db,
                parser_registry=FakeParserRegistry(),
                chunk_engine=FakeChunkEngine(),
                embedding_engine=FakeEmbeddingEngine(),
                fulltext_required=True,
                minio_storage=FakeProcessingMinio(),
                vector_store=FakeVectorStore(),
                layer_store=None,
                chunk_fulltext_store=None,
                chunk_index_mode="legacy",
            )

        db.refresh(file)
        assert file.processing_status != ProcessingStatus.completed
        assert db.query(DocumentChunk).count() == 0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.mark.parametrize("chunk_index_mode", ["legacy", "v2_alias"])
def test_required_document_processing_fails_on_partial_fulltext_write(
    tmp_path, chunk_index_mode
):
    engine, db = _new_db()
    try:
        user, _workspace, file = _seed_file(db)
        source_path = tmp_path / "report.pdf"
        source_path.write_bytes(b"source document bytes")
        processor = DocumentProcessor(
            db=db,
            parser_registry=FakeParserRegistry(),
            chunk_engine=FakeChunkEngine(),
            embedding_engine=FakeEmbeddingEngine(),
            minio_storage=FakeProcessingMinio(),
            vector_store=FakeVectorStore(),
            layer_store=None,
            chunk_fulltext_store=FakeV2EsStore(upsert_count=2),
            chunk_index_mode=chunk_index_mode,
            fulltext_required=True,
        )

        with pytest.raises(FulltextIndexingError, match="partial_write:2/3"):
            processor.process_document(
                file_path=str(source_path),
                file_id=file.id,
                user_id=user.id,
                parser_type="pdf",
            )

        db.refresh(file)
        assert file.processing_status != ProcessingStatus.completed
        assert db.query(DocumentChunk).count() == 0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_upload_ingest_records_upload_trace_spans(monkeypatch):
    engine, db = _new_db()
    try:
        user, workspace, _ = _seed_file(db)
        fake_minio = FakeUploadMinio()
        monkeypatch.setattr(file_ingest, "MinioStorage", lambda: fake_minio)

        set_trace_context(trace_id="upload-trace-1", sampling_reason="unit-test")
        file_record, task_record = file_ingest.ingest_new_file(
            db,
            workspace,
            user.id,
            parent_logical_path="/uploads",
            upload_filename="new.txt",
            file_content=b"hello",
            content_type="text/plain",
            parser_type="auto",
        )

        assert file_record.id
        assert task_record is not None
        run = db.query(TraceRun).filter_by(trace_id="upload-trace-1").one()
        assert run.trace_type == "upload"
        assert run.workspace_id == workspace.id
        assert run.user_id == user.id
        assert run.file_id == file_record.id
        assert run.status == "success"
        spans = {
            span.stage: span for span in db.query(TraceSpan).order_by(TraceSpan.id)
        }
        assert set(spans) == {
            "upload.validate",
            "upload.store_minio",
            "upload.create_records",
        }
        assert spans["upload.validate"].output_summary["file_size"] == 5
        assert (
            spans["upload.store_minio"].output_summary["object_key"]
            == "/uploads/new.txt"
        )
        assert spans["upload.create_records"].output_summary["task_created"] is True
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_upload_trace_failure_is_best_effort(monkeypatch):
    engine, db = _new_db()
    try:
        user, workspace, _ = _seed_file(db)
        fake_minio = FakeUploadMinio()
        monkeypatch.setattr(file_ingest, "MinioStorage", lambda: fake_minio)

        def boom(*args, **kwargs):
            raise RuntimeError("trace database down")

        monkeypatch.setattr(
            "openrag.services.trace_service.TraceService.start_span", boom
        )

        file_record, _ = file_ingest.ingest_new_file(
            db,
            workspace,
            user.id,
            parent_logical_path="/uploads",
            upload_filename="best-effort.txt",
            file_content=b"hello",
            content_type="text/plain",
        )

        assert file_record.id
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_worker_document_processing_records_trace_and_canonical_artifacts(monkeypatch):
    engine, db = _new_db()
    try:
        user, workspace, file = _seed_file(db)
        user_id = user.id
        workspace_id = workspace.id
        file_id = file.id
        db.close()
        task_worker.SessionLocal.configure(bind=engine)
        processing_minio = FakeProcessingMinio()
        monkeypatch.setattr(task_worker, "MinioStorage", lambda: processing_minio)
        monkeypatch.setattr(task_worker, "ParserRegistry", FakeParserRegistry)
        monkeypatch.setattr(task_worker, "ChunkEngine", FakeChunkEngine)
        monkeypatch.setattr(task_worker, "HierarchyStorage", lambda: object())
        monkeypatch.setenv("OPENRAG_CHUNK_SIZE", "600")
        monkeypatch.setenv("OPENRAG_CHUNK_OVERLAP", "80")
        monkeypatch.setenv("OPENRAG_MIN_CHUNK_TOKENS", "2")

        set_trace_context(
            trace_id="processing-trace-1",
            trace_type="document_processing",
            workspace_id=workspace_id,
            user_id=user_id,
            file_id=file_id,
            task_id="77",
            sampling_reason="unit-test",
        )

        worker = task_worker.TaskWorker()
        worker.parser_registry = FakeParserRegistry()
        worker.chunk_engine = FakeChunkEngine()
        worker.embedding_engine = FakeEmbeddingEngine()
        worker.hierarchy_storage = object()
        worker.vector_store = FakeVectorStore()
        worker.layer_store = None
        es_store = FakeV2EsStore()
        worker.chunk_fulltext_store = es_store
        worker.chunk_index_mode = "v2_alias"
        worker.fulltext_required = True
        worker.require_layer_vectors = False
        runtime = IndexRuntime(
            snapshot=SimpleNamespace(
                generation_id="generation-test",
                state="active",
                embedding_fingerprint="a" * 64,
                chunk_collection_name="fake_chunks",
                layer_collection_name=None,
            ),
            embedding_engine=worker.embedding_engine,
            vector_store=worker.vector_store,
            layer_store=None,
        )
        result = worker._process_document(
            {
                "file_id": file_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "index_generation_id": "generation-test",
            },
            task_id=77,
            runtime=runtime,
        )

        db2 = sessionmaker(bind=engine)()
        try:
            assert result["status"] == "success"
            run = db2.query(TraceRun).filter_by(trace_id="processing-trace-1").one()
            assert run.status == "success"
            stages = {
                span.stage: span for span in db2.query(TraceSpan).order_by(TraceSpan.id)
            }
            assert set(stages) == {
                "worker.download_file",
                "parse.document",
                "parsed_artifacts.persist",
                "chunk.build",
                "embedding.chunks",
                "vector.milvus_insert",
                "fulltext.es_index",
                "storage.save_chunks",
                "metadata.persist_chunks",
            }
            parse_span = stages["parse.document"]
            assert parse_span.duration_ms is not None
            assert parse_span.output_summary["parser_name"] == "FakeParser"
            assert parse_span.output_summary["block_count"] == 2
            assert "SECRET FULL PARSE BODY" not in json.dumps(parse_span.output_summary)

            artifact_span = stages["parsed_artifacts.persist"]
            assert artifact_span.output_summary["status"] == "completed"
            assert artifact_span.output_summary["canonical_json_object_key"].endswith(
                "/canonical.json"
            )
            assert artifact_span.output_summary["canonical_md_object_key"].endswith(
                "/canonical.md"
            )
            assert artifact_span.output_summary["block_count"] == 2
            assert artifact_span.output_summary["page_count"] == 2
            assert "SECRET FULL PARSE BODY" not in json.dumps(
                artifact_span.output_summary
            )

            assert stages["chunk.build"].output_summary["chunk_count"] == 3
            assert stages["chunk.build"].output_summary["short_chunk_count"] == 2
            assert stages["chunk.build"].output_summary["empty_chunk_count"] == 0
            assert stages["embedding.chunks"].output_summary == {
                "embedding_model": "fake-embedding",
                "dimension": 3,
                "batch_size": 3,
                "batch_count": 1,
                "chunk_count": 3,
                "success_count": 3,
                "failure_count": 0,
            }
            assert stages["vector.milvus_insert"].output_summary == {
                "insert_count": 3,
                "collection": "fake_chunks",
                "failure_reason": None,
            }
            assert stages["fulltext.es_index"].output_summary["doc_count"] == 3
            assert stages["fulltext.es_index"].output_summary["upsert_count"] == 3
            assert stages["fulltext.es_index"].output_summary["fulltext_required"] is True
            assert stages["fulltext.es_index"].output_summary["chunk_index_mode"] == "v2_alias"
            assert stages["fulltext.es_index"].output_summary["expected_doc_count"] == 3
            assert stages["fulltext.es_index"].output_summary["submitted_doc_count"] == 3
            assert stages["fulltext.es_index"].output_summary["accepted_doc_count"] == 3
            assert (
                stages["metadata.persist_chunks"].output_summary[
                    "document_chunks_written"
                ]
                == 3
            )
            assert stages["fulltext.es_index"].output_summary["es_delete_performed"] is True
            assert stages["fulltext.es_index"].output_summary["es_write_complete"] is True
            assert [operation[0] for operation in es_store.operations] == ["delete", "bulk"]
            first_es_doc = es_store.docs[0]
            assert first_es_doc["content"] == "Heading body GB/T 35273-2020"
            assert first_es_doc["content_with_weight"] == first_es_doc["content"]
            assert first_es_doc["exact_terms"] == ["gb/t 35273-2020"]
            assert first_es_doc["mom_with_weight"] == "parent compatibility text"
            assert "content_ltks" not in first_es_doc
            assert "content_sm_ltks" not in first_es_doc
            assert stages["fulltext.es_index"].output_summary["schema_version"] == (
                "a02-content-exact-v2"
            )
            assert len(
                stages["fulltext.es_index"].output_summary["mapping_hash"]
            ) == 64
            assert db2.query(DocumentChunk).count() == 3
            first_chunk = db2.query(DocumentChunk).filter_by(chunk_id="chunk-1").one()
            assert first_chunk.page_num_int == [1, 1]
            assert first_chunk.position_int == [
                [1, 10, 100, 20, 40],
                [1, 10, 100, 45, 65],
            ]
            assert first_chunk.top_int == [20, 45]
            assert db2.query(DocumentParseArtifact).count() == 1
            keys = [key for (_bucket, key) in processing_minio.objects]
            assert any(key.endswith("/canonical.json") for key in keys)
            assert any(key.endswith("/canonical.md") for key in keys)
            assert not any(
                "blocks" in json.dumps(span.output_summary or {}).lower()
                for span in stages.values()
            )
            assert not any(
                "embedding" in json.dumps(span.output_summary or {}).lower()
                and "[0.1" in json.dumps(span.output_summary or {})
                for span in stages.values()
            )
        finally:
            db2.close()
    finally:
        Base.metadata.drop_all(bind=engine)


def test_required_legacy_es_partial_write_fails_closed(monkeypatch):
    engine, db = _new_db()
    try:
        user, workspace, file = _seed_file(db)
        ids = (user.id, workspace.id, file.id)
        db.close()
        task_worker.SessionLocal.configure(bind=engine)
        monkeypatch.setattr(task_worker, "MinioStorage", FakeProcessingMinio)
        worker = task_worker.TaskWorker()
        worker.parser_registry = FakeParserRegistry()
        worker.chunk_engine = FakeChunkEngine()
        worker.embedding_engine = FakeEmbeddingEngine()
        worker.hierarchy_storage = object()
        worker.vector_store = FakeVectorStore()
        worker.layer_store = None
        worker.chunk_fulltext_store = FakeEsStore(upsert_count=2)
        worker.chunk_index_mode = "legacy"
        worker.fulltext_required = True
        worker.require_layer_vectors = False
        runtime = IndexRuntime(
            snapshot=SimpleNamespace(
                generation_id="generation-test",
                state="active",
                embedding_fingerprint="a" * 64,
                chunk_collection_name="fake_chunks",
                layer_collection_name=None,
            ),
            embedding_engine=worker.embedding_engine,
            vector_store=worker.vector_store,
            layer_store=None,
        )

        trace_id = "required-legacy-es-partial"
        set_trace_context(
            trace_id=trace_id,
            trace_type="document_processing",
            workspace_id=ids[1],
            user_id=ids[0],
            file_id=ids[2],
            task_id="a01",
            sampling_reason="unit-test",
        )
        with pytest.raises(FulltextIndexingError, match="partial_write:2/3"):
            worker._process_document(
                {
                    "file_id": ids[2],
                    "workspace_id": ids[1],
                    "user_id": ids[0],
                    "index_generation_id": "generation-test",
                },
                task_id=78,
                runtime=runtime,
            )

        db2 = sessionmaker(bind=engine)()
        try:
            stored_file = db2.get(File, ids[2])
            assert stored_file.processing_status != ProcessingStatus.completed
            assert db2.query(DocumentChunk).count() == 0
            span = db2.query(TraceSpan).filter_by(
                trace_id=trace_id, stage="fulltext.es_index"
            ).one()
            assert span.output_summary["fulltext_required"] is True
            assert span.output_summary["chunk_index_mode"] == "legacy"
            assert span.output_summary["expected_doc_count"] == 3
            assert span.output_summary["submitted_doc_count"] == 3
            assert span.output_summary["accepted_doc_count"] == 2
            assert span.output_summary["failure_reason"] == "partial_write:2/3"
        finally:
            db2.close()
    finally:
        Base.metadata.drop_all(bind=engine)


_PROFILE_OK = {
    "total_ms": 123,
    "page_count": 2,
    "n_tables": 1,
    "status": "ok",
    "stages": [
        {"stage": "pdf.images_ocr", "duration_ms": 100},
        {"stage": "pdf.layout_recognition", "duration_ms": 23},
    ],
}


class FakeProfiledParser:
    parser_version = "pdf-test"

    def __init__(self):
        self.last_parse_profile = dict(_PROFILE_OK)

    def parse(self, file_path):
        return [
            DocumentBlock(
                text="Body",
                page=1,
                offset=0,
                block_type="text",
                block_id="b1",
                char_start=0,
                char_end=4,
            )
        ]


class FakeProfiledRegistry:
    def get_parser(self, file_path, parser_type):
        return FakeProfiledParser()


class FakeFailingProfiledParser:
    parser_version = "pdf-test"

    def __init__(self):
        # __call__ writes this in its own finally before raising; the adapter
        # copies it to last_parse_profile even on failure.
        self.last_parse_profile = {
            "total_ms": 50,
            "page_count": 1,
            "n_tables": 0,
            "status": "error",
            "stages": [{"stage": "pdf.images_ocr", "duration_ms": 50}],
        }

    def parse(self, file_path):
        raise RuntimeError("layout boom")


class FakeFailingProfiledRegistry:
    def get_parser(self, file_path, parser_type):
        return FakeFailingProfiledParser()


def test_process_document_persists_pdf_stage_profile_on_failure(tmp_path):
    engine, db = _new_db()
    try:
        user, workspace, file = _seed_file(db)
        source = tmp_path / "report.pdf"
        source.write_bytes(b"source document bytes")
        processor = DocumentProcessor(
            db=db,
            parser_registry=FakeFailingProfiledRegistry(),
            chunk_engine=FakeChunkEngine(),
            embedding_engine=FakeEmbeddingEngine(),
            fulltext_required=False,
            minio_storage=FakeProcessingMinio(),
            vector_store=FakeVectorStore(),
            layer_store=None,
            chunk_fulltext_store=None,
        )
        set_trace_context(
            trace_id="proc-profile-fail",
            trace_type="document_processing",
            workspace_id=workspace.id,
            user_id=user.id,
            file_id=file.id,
            task_id="1",
            sampling_reason="unit-test",
        )
        with pytest.raises(RuntimeError):
            processor.process_document(
                file_path=str(source),
                file_id=file.id,
                user_id=user.id,
                parser_type="pdf",
            )
        parse_span = db.query(TraceSpan).filter_by(stage="parse.document").one()
        assert parse_span.status == "failed"
        prof = parse_span.output_summary["pdf_stage_profile"]
        assert prof["status"] == "error"
        assert prof["stages"][0]["stage"] == "pdf.images_ocr"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_process_document_persists_pdf_stage_profile_on_success(tmp_path):
    engine, db = _new_db()
    try:
        user, workspace, file = _seed_file(db)
        source = tmp_path / "report.pdf"
        source.write_bytes(b"source document bytes")
        processor = DocumentProcessor(
            db=db,
            parser_registry=FakeProfiledRegistry(),
            chunk_engine=FakeChunkEngine(),
            embedding_engine=FakeEmbeddingEngine(),
            fulltext_required=False,
            minio_storage=FakeProcessingMinio(),
            vector_store=FakeVectorStore(),
            layer_store=None,
            chunk_fulltext_store=None,
        )
        set_trace_context(
            trace_id="proc-profile-ok",
            trace_type="document_processing",
            workspace_id=workspace.id,
            user_id=user.id,
            file_id=file.id,
            task_id="1",
            sampling_reason="unit-test",
        )
        result = processor.process_document(
            file_path=str(source),
            file_id=file.id,
            user_id=user.id,
            parser_type="pdf",
        )
        assert result["status"] == "completed"
        parse_span = db.query(TraceSpan).filter_by(stage="parse.document").one()
        prof = parse_span.output_summary["pdf_stage_profile"]
        assert prof["status"] == "ok"
        assert prof["total_ms"] == 123
        assert [s["stage"] for s in prof["stages"]] == [
            "pdf.images_ocr",
            "pdf.layout_recognition",
        ]
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
