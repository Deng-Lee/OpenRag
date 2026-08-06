"""Document completion gate tests for required vector artifacts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.openrag.processors.document_processor import (
    DocumentProcessor,
    FulltextIndexingError,
)
from src.openrag.vectorstore.errors import VectorWriteIncompleteError


def constructor_kwargs(**overrides):
    values = {
        "db": Mock(),
        "parser_registry": Mock(),
        "chunk_engine": Mock(),
        "embedding_engine": Mock(),
        "fulltext_required": False,
        "vector_store": Mock(),
        "layer_store": None,
    }
    values.update(overrides)
    return values


def test_vector_store_is_required():
    with pytest.raises(VectorWriteIncompleteError):
        DocumentProcessor(**constructor_kwargs(vector_store=None))


def test_layer_store_is_required_when_layer_vectors_enabled():
    with pytest.raises(VectorWriteIncompleteError):
        DocumentProcessor(**constructor_kwargs(require_layer_vectors=True))


def test_layer_vectors_are_generated_before_store_receives_them():
    embedding_engine = Mock()
    embedding_engine.embed_batch.return_value = [[1.0, 1.0], [2.0, 2.0]]
    processor = DocumentProcessor(
        **constructor_kwargs(
            embedding_engine=embedding_engine,
            layer_store=Mock(),
            require_layer_vectors=True,
        )
    )

    result = processor._build_layer_embeddings(
        SimpleNamespace(l0="summary", l1="overview")
    )

    assert result == [
        ("l0", "summary", [1.0, 1.0]),
        ("l1", "overview", [2.0, 2.0]),
    ]
    embedding_engine.embed_batch.assert_called_once_with(["summary", "overview"])


def test_fulltext_indexing_failure_is_retryable_and_has_stable_error_code():
    error = FulltextIndexingError("partial_write:2/3")

    assert error.code == "FULLTEXT_INDEXING_FAILED"
    assert error.public_message == "Elasticsearch chunk indexing failed"
    assert error.retryable is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("chunk_embedding_count", 1),
        ("milvus_chunk_insert_count", 1),
        ("document_chunk_metadata_count", 1),
        ("milvus_layer_insert_count", 0),
    ],
)
def test_any_incomplete_required_count_blocks_completion(field, value):
    counts = {
        "chunk_count": 2,
        "chunk_embedding_count": 2,
        "milvus_chunk_insert_count": 2,
        "document_chunk_metadata_count": 2,
        "expected_layer_count": 1,
        "milvus_layer_insert_count": 1,
    }
    counts[field] = value

    with pytest.raises(VectorWriteIncompleteError):
        DocumentProcessor._assert_processing_complete(**counts)


def test_matching_required_counts_allow_completion():
    DocumentProcessor._assert_processing_complete(
        chunk_count=2,
        chunk_embedding_count=2,
        milvus_chunk_insert_count=2,
        document_chunk_metadata_count=2,
        expected_layer_count=1,
        milvus_layer_insert_count=1,
    )
