from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401 - register models
from openrag.chunking.chunk_engine import ChunkEngine, ChunkStrategy
from openrag.chunking.ragflow_core.semantic import chunk_semantic_ragflow
from openrag.chunking.chunk_models import Chunk
from openrag.models.base import Base
from openrag.models.file import File
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.indexing.runtime import IndexRuntime
from openrag.parsers.base import DocumentBlock
from openrag.processors.document_processor import DocumentProcessor
from openrag.worker import task_worker


class FakeParser:
    parser_version = "test"

    def parse(self, file_path):
        return [
            DocumentBlock(
                text=Path(file_path).read_text(encoding="utf-8"),
                page=1,
                offset=0,
                block_type="text",
                level=0,
            )
        ]


class FakeParserRegistry:
    def get_parser(self, file_path, parser_type):
        return FakeParser()


class RecordingChunkEngine:
    def __init__(self):
        self.document_type = None

    def chunk(
        self,
        text_blocks,
        *,
        chunk_size,
        chunk_overlap,
        chunk_method=None,
        min_chunk_tokens=0,
        document_type,
    ):
        self.document_type = document_type
        return [
            Chunk(
                text=text_blocks[0].text,
                chunk_id="chunk-doc-type",
                page=1,
                block_type="text",
                metadata={"document_type": document_type},
            )
        ]


class FakeEmbeddingEngine:
    model_name = "fake"
    dimension = 3

    def embed_chunks(self, chunks):
        return [(chunk, [0.1, 0.2, 0.3]) for chunk in chunks]

    def embed_text(self, text):
        return [0.1, 0.2, 0.3]


class FakeHierarchyStorage:
    def save_document_hierarchy(self, file_uri, l0, l1, l2):
        self.saved = (file_uri, l0, l1, l2)

    def get_l0_path(self, file_uri):
        return f"/fake/{file_uri.lstrip('/')}/l0.md"

    def get_l1_path(self, file_uri):
        return f"/fake/{file_uri.lstrip('/')}/l1.md"

    def get_l2_path(self, file_uri):
        return f"/fake/{file_uri.lstrip('/')}/chunks"

    def get_chunk_file_path(self, file_uri, index):
        return f"/fake/{file_uri.lstrip('/')}/chunks/{index:04d}.md"


class FakeDownloadMinio:
    def get_file_to_path(self, bucket_name, object_name, file_path):
        Path(file_path).write_text("worker text", encoding="utf-8")


class FakeVectorStore:
    def delete_by_file_id(self, file_id):
        pass

    def insert_chunks(self, file_id, chunk_embeddings, workspace_id=None):
        return len(chunk_embeddings)


class CapturingProcessor:
    document_type = None

    def __init__(self, **kwargs):
        pass

    def process_document(
        self,
        *,
        file_path,
        file_id,
        user_id,
        parser_type,
        document_type,
        progress_callback,
    ):
        self.__class__.document_type = document_type
        return {
            "file_id": file_id,
            "parser_type": parser_type,
            "document_type": document_type,
            "status": "completed",
        }


def _new_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _seed_file(db, document_type="general"):
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
        uri="/docs/source.txt",
        name="source.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=32,
        mime_type="text/plain",
        document_type=document_type,
    )
    db.add(file)
    db.commit()
    db.refresh(file)
    return user, workspace, file


def test_normalize_document_type_defaults_and_valid_values():
    from openrag.chunking.document_type import normalize_document_type

    assert normalize_document_type(None) == "general"
    assert normalize_document_type("") == "general"
    assert normalize_document_type(" Manual ") == "manual"
    assert normalize_document_type("LAWS") == "laws"


def test_normalize_document_type_rejects_unknown_value():
    from openrag.chunking.document_type import normalize_document_type

    with pytest.raises(ValueError, match="document_type"):
        normalize_document_type("contract")


def test_chunk_engine_passes_document_type_to_semantic_metadata():
    engine = ChunkEngine(strategy=ChunkStrategy.SEMANTIC)
    blocks = [
        DocumentBlock(
            text="alpha beta",
            page=1,
            offset=0,
            block_type="text",
            level=0,
        )
    ]

    chunks = engine.chunk(blocks, chunk_size=512, chunk_overlap=0, document_type="laws")

    assert chunks
    assert all(chunk.metadata["document_type"] == "laws" for chunk in chunks)


def test_semantic_direct_call_preserves_chunks_and_sets_document_type_metadata():
    blocks = [
        DocumentBlock(
            text="alpha beta",
            page=1,
            offset=0,
            block_type="text",
            level=0,
        )
    ]

    general_chunks = chunk_semantic_ragflow(
        blocks,
        512,
        0,
        None,
        fixed_size_fallback=lambda text_blocks, chunk_size, chunk_overlap: [],
        document_type="general",
    )
    manual_chunks = chunk_semantic_ragflow(
        blocks,
        512,
        0,
        None,
        fixed_size_fallback=lambda text_blocks, chunk_size, chunk_overlap: [],
        document_type="manual",
    )

    assert [chunk.text for chunk in manual_chunks] == [
        chunk.text for chunk in general_chunks
    ]
    assert len(manual_chunks) == len(general_chunks)
    assert all(chunk.metadata["document_type"] == "manual" for chunk in manual_chunks)


def test_document_processor_passes_document_type_to_chunk_engine(tmp_path, monkeypatch):
    engine, db = _new_db()
    try:
        user, workspace, file = _seed_file(db, document_type="manual")
        source = tmp_path / "source.txt"
        source.write_text("processor text", encoding="utf-8")
        chunk_engine = RecordingChunkEngine()
        progress_values = []
        monkeypatch.setenv("OPENRAG_CHUNK_SIZE", "600")
        monkeypatch.setenv("OPENRAG_CHUNK_OVERLAP", "80")

        processor = DocumentProcessor(
            db=db,
            parser_registry=FakeParserRegistry(),
            chunk_engine=chunk_engine,
            embedding_engine=FakeEmbeddingEngine(),
            fulltext_required=False,
            hierarchy_storage=FakeHierarchyStorage(),
            minio_storage=None,
            vector_store=FakeVectorStore(),
            layer_store=None,
            chunk_fulltext_store=None,
        )

        result = processor.process_document(
            file_path=str(source),
            file_id=file.id,
            user_id=user.id,
            parser_type="auto",
            document_type="manual",
            progress_callback=progress_values.append,
        )

        assert chunk_engine.document_type == "manual"
        assert result["document_type"] == "manual"
        assert progress_values == [5, 20, 35, 50, 65, 80, 90]
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_worker_reads_file_document_type_and_returns_it(monkeypatch):
    engine, db = _new_db()
    try:
        user, workspace, file = _seed_file(db, document_type="laws")
        user_id = user.id
        workspace_id = workspace.id
        file_id = file.id
        db.close()

        task_worker.SessionLocal.configure(bind=engine)
        CapturingProcessor.document_type = None
        monkeypatch.setattr(task_worker, "MinioStorage", lambda: FakeDownloadMinio())
        monkeypatch.setattr(task_worker, "ParserRegistry", lambda: object())
        monkeypatch.setattr(task_worker, "ChunkEngine", lambda: object())
        monkeypatch.setattr(task_worker, "HierarchyStorage", lambda: object())
        monkeypatch.setattr(task_worker, "DocumentProcessor", CapturingProcessor)

        worker = task_worker.TaskWorker()
        worker.parser_registry = object()
        worker.chunk_engine = object()
        worker.embedding_engine = object()
        worker.hierarchy_storage = object()
        worker.vector_store = object()
        worker.layer_store = None
        worker.chunk_fulltext_store = None
        worker.require_layer_vectors = False
        runtime = IndexRuntime(
            snapshot=SimpleNamespace(
                generation_id="generation-test",
                state="active",
                embedding_fingerprint="a" * 64,
                chunk_collection_name="fake_chunks",
                layer_collection_name=None,
            ),
            embedding_engine=SimpleNamespace(assert_fingerprint=lambda _: None),
            vector_store=SimpleNamespace(collection_name="fake_chunks"),
            layer_store=None,
        )
        result = worker._process_document(
            {
                "file_id": file_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "index_generation_id": "generation-test",
            },
            task_id=88,
            runtime=runtime,
        )

        assert CapturingProcessor.document_type == "laws"
        assert result["document_type"] == "laws"
    finally:
        Base.metadata.drop_all(bind=engine)
