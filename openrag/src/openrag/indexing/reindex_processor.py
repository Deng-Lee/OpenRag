"""File-level candidate rebuild from immutable canonical sources."""

from dataclasses import dataclass

from openrag.indexing.runtime import IndexRuntime
from openrag.indexing.source_reader import GenerationSourceReader
from openrag.vectorstore.errors import VectorWriteIncompleteError


class SourceRevisionChangedError(RuntimeError):
    code = "SOURCE_REVISION_CHANGED"
    retryable = True


@dataclass(frozen=True)
class ReindexFileResult:
    file_id: int
    source_content_hash: str
    expected_chunk_count: int
    written_chunk_count: int
    expected_layer_count: int
    written_layer_count: int


class GenerationReindexProcessor:
    def __init__(self, source_reader: GenerationSourceReader, runtime: IndexRuntime):
        self.source_reader = source_reader
        self.runtime = runtime

    def reindex_file(
        self, file_id: int, *, expected_source_content_hash: str
    ) -> ReindexFileResult:
        source = self.source_reader.get_source_revision(file_id)
        if source.source_content_hash != expected_source_content_hash:
            raise SourceRevisionChangedError("Canonical source changed before rebuild")
        self.runtime.embedding_engine.assert_fingerprint(
            self.runtime.snapshot.embedding_fingerprint
        )
        chunk_embeddings = self._embed_chunks(list(source.chunks))
        layer_embeddings = self._embed_layers(list(source.layers))
        written_chunks, written_layers = self._replace_candidate_vectors(
            file_id,
            source.workspace_id,
            chunk_embeddings,
            layer_embeddings,
        )
        self._verify_file_counts(
            len(source.chunks),
            written_chunks,
            len(source.layers),
            written_layers,
        )
        return ReindexFileResult(
            file_id=file_id,
            source_content_hash=source.source_content_hash,
            expected_chunk_count=len(source.chunks),
            written_chunk_count=written_chunks,
            expected_layer_count=len(source.layers),
            written_layer_count=written_layers,
        )

    def _embed_chunks(self, chunks):
        values = self.runtime.embedding_engine.embed_chunks(chunks)
        if len(values) != len(chunks):
            raise VectorWriteIncompleteError("Candidate chunk embedding count mismatch")
        return values

    def _embed_layers(self, layers):
        if not layers:
            return []
        values = self.runtime.embedding_engine.embed_batch([text for _, text in layers])
        if len(values) != len(layers):
            raise VectorWriteIncompleteError("Candidate layer embedding count mismatch")
        return [(name, text, vector) for (name, text), vector in zip(layers, values)]

    def _replace_candidate_vectors(
        self, file_id, workspace_id, chunk_embeddings, layer_embeddings
    ):
        self.runtime.vector_store.delete_by_file_id(file_id)
        if self.runtime.layer_store is not None:
            self.runtime.layer_store.delete_by_file_id(file_id)
        written_chunks = self.runtime.vector_store.insert_chunks(
            file_id, chunk_embeddings, workspace_id=workspace_id
        )
        if layer_embeddings:
            if self.runtime.layer_store is None:
                raise VectorWriteIncompleteError(
                    "Candidate layer Collection is required for canonical layers"
                )
            written_layers = self.runtime.layer_store.upsert_file_layers(
                file_id, layer_embeddings, workspace_id=workspace_id
            )
        else:
            written_layers = 0
        return written_chunks, written_layers

    @staticmethod
    def _verify_file_counts(
        expected_chunks, written_chunks, expected_layers, written_layers
    ):
        if expected_chunks <= 0 or written_chunks != expected_chunks:
            raise VectorWriteIncompleteError("Candidate chunk write count mismatch")
        if written_layers != expected_layers:
            raise VectorWriteIncompleteError("Candidate layer write count mismatch")
