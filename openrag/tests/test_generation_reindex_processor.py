from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.reindex_processor import (
    GenerationReindexProcessor,
    SourceRevisionChangedError,
)
from openrag.indexing.runtime import IndexRuntime
from openrag.indexing.source_reader import GenerationSourceReader
from openrag.embedding.errors import EmbeddingProviderError
from openrag.models import Base, DocumentChunk
from openrag.models.file import File, ProcessingStatus
from openrag.models.user import User
from openrag.models.workspace import Workspace


class ReadOnlyMinio:
    def __init__(self, chunks, l0="summary", l1="overview"):
        self.chunks = chunks
        self.l0 = l0
        self.l1 = l1

    def get_object_text(self, bucket, key):
        return self.chunks.get((bucket, key))

    def read_hierarchy_abstract(self, _bucket, _uri):
        return self.l0

    def read_hierarchy_overview(self, _bucket, _uri):
        return self.l1


class NoLocalWrites:
    def load_l0(self, **_kwargs):
        return None

    def load_l1(self, **_kwargs):
        return None


class Engine:
    def __init__(self):
        self.seen_chunk_texts = []

    def assert_fingerprint(self, fingerprint):
        assert fingerprint == "b" * 64

    def embed_chunks(self, chunks):
        self.seen_chunk_texts = [chunk.text for chunk in chunks]
        return [(chunk, [1.0, 0.0, 0.0]) for chunk in chunks]

    def embed_batch(self, texts):
        return [[0.0, 1.0, 0.0] for _ in texts]


class ChunkStore:
    collection_name = "chunks_candidate"

    def __init__(self):
        self.rows = {}

    def delete_by_file_id(self, file_id):
        self.rows.pop(file_id, None)

    def insert_chunks(self, file_id, pairs, workspace_id=None):
        assert workspace_id == 1
        self.rows[file_id] = list(pairs)
        return len(pairs)


class LayerStore:
    collection_name = "layers_candidate"

    def __init__(self):
        self.rows = {}

    def delete_by_file_id(self, file_id):
        self.rows.pop(file_id, None)

    def upsert_file_layers(self, file_id, values, workspace_id=None):
        assert workspace_id == 1
        self.rows[file_id] = list(values)
        return len(values)


@pytest.fixture
def source_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="source-user",
        email="source@example.com",
        password_hash="hash",
        full_name="Source User",
        is_active=True,
    )
    db.add(user)
    db.flush()
    workspace = Workspace(
        name="Source Workspace",
        slug="source-workspace",
        owner_id=user.id,
    )
    db.add(workspace)
    db.flush()
    file = File(
        uri="docs/source.txt",
        name="source.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=100,
        processing_status=ProcessingStatus.completed,
    )
    db.add(file)
    db.flush()
    for index in range(2):
        db.add(
            DocumentChunk(
                id=index + 1,
                file_id=file.id,
                workspace_id=workspace.id,
                chunk_id=f"chunk-{index}",
                chunk_index=index,
                object_key=f"chunks/{index}.md",
                text_preview="truncated",
                page=index + 1,
                level=0,
                block_type="text",
                start_offset=0,
                end_offset=10,
            )
        )
    db.commit()
    yield db, file
    db.close()
    Base.metadata.drop_all(engine)


def runtime():
    engine = Engine()
    chunks = ChunkStore()
    layers = LayerStore()
    return IndexRuntime(
        snapshot=SimpleNamespace(
            generation_id="candidate",
            embedding_fingerprint="b" * 64,
            chunk_collection_name=chunks.collection_name,
            layer_collection_name=layers.collection_name,
        ),
        embedding_engine=engine,
        vector_store=chunks,
        layer_store=layers,
    )


def test_reindex_reads_full_canonical_text_and_only_replaces_candidate_vectors(
    source_db,
):
    db, file = source_db
    full_text = "FULL-TEXT-" * 100
    minio = ReadOnlyMinio(
        {
            ("source-workspace", "chunks/0.md"): full_text,
            ("source-workspace", "chunks/1.md"): "second full chunk",
        }
    )
    reader = GenerationSourceReader(
        db, minio_storage=minio, hierarchy_storage=NoLocalWrites()
    )
    target = runtime()
    source = reader.get_source_revision(file.id)
    file_state = (file.processing_status, file.processing_error, file.updated_at)
    chunk_state = [
        (row.chunk_id, row.text_preview, row.object_key)
        for row in db.query(DocumentChunk).order_by(DocumentChunk.chunk_index)
    ]

    first = GenerationReindexProcessor(reader, target).reindex_file(
        file.id, expected_source_content_hash=source.source_content_hash
    )
    second = GenerationReindexProcessor(reader, target).reindex_file(
        file.id, expected_source_content_hash=source.source_content_hash
    )

    db.refresh(file)
    assert target.embedding_engine.seen_chunk_texts[0] == full_text
    assert first.written_chunk_count == second.written_chunk_count == 2
    assert first.written_layer_count == second.written_layer_count == 2
    assert len(target.vector_store.rows[file.id]) == 2
    assert len(target.layer_store.rows[file.id]) == 2
    assert (
        file.processing_status,
        file.processing_error,
        file.updated_at,
    ) == file_state
    assert [
        (row.chunk_id, row.text_preview, row.object_key)
        for row in db.query(DocumentChunk).order_by(DocumentChunk.chunk_index)
    ] == chunk_state


def test_source_hash_change_fails_before_embedding_or_candidate_write(source_db):
    db, file = source_db
    minio = ReadOnlyMinio(
        {
            ("source-workspace", "chunks/0.md"): "one",
            ("source-workspace", "chunks/1.md"): "two",
        }
    )
    reader = GenerationSourceReader(
        db, minio_storage=minio, hierarchy_storage=NoLocalWrites()
    )
    target = runtime()

    with pytest.raises(SourceRevisionChangedError):
        GenerationReindexProcessor(reader, target).reindex_file(
            file.id, expected_source_content_hash="outdated"
        )

    assert target.embedding_engine.seen_chunk_texts == []
    assert target.vector_store.rows == {}


def test_candidate_provider_failure_does_not_mark_online_file_failed(source_db):
    db, file = source_db
    minio = ReadOnlyMinio(
        {
            ("source-workspace", "chunks/0.md"): "one",
            ("source-workspace", "chunks/1.md"): "two",
        }
    )
    reader = GenerationSourceReader(
        db, minio_storage=minio, hierarchy_storage=NoLocalWrites()
    )
    target = runtime()
    source = reader.get_source_revision(file.id)

    def fail(_chunks):
        raise EmbeddingProviderError(
            "EMBEDDING_PROVIDER_UNAVAILABLE",
            "candidate unavailable",
            retryable=True,
        )

    target.embedding_engine.embed_chunks = fail
    with pytest.raises(EmbeddingProviderError):
        GenerationReindexProcessor(reader, target).reindex_file(
            file.id, expected_source_content_hash=source.source_content_hash
        )

    db.refresh(file)
    assert file.processing_status == ProcessingStatus.completed
    assert file.processing_error is None
    assert target.vector_store.rows == {}
