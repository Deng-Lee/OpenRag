"""Milvus writes reject invalid or incomplete batches without partial success."""

from types import SimpleNamespace

import pytest

from src.openrag.chunking.chunk_models import Chunk
from src.openrag.vectorstore.errors import (
    VectorSchemaMismatchError,
    VectorWriteIncompleteError,
)
from src.openrag.vectorstore.milvus_layer_store import MilvusLayerStore
from src.openrag.vectorstore.milvus_store import MilvusStore


class FakeCollection:
    def __init__(self, insert_count=None):
        self.insert_count = insert_count
        self.insert_calls = 0
        self.delete_calls = 0
        self.last_data = None

    def insert(self, data):
        self.insert_calls += 1
        self.last_data = data
        count = self.insert_count
        if count is None:
            count = len(data[0])
        return SimpleNamespace(insert_count=count)

    def delete(self, expr):
        self.delete_calls += 1


def chunk_store(insert_count=None):
    store = object.__new__(MilvusStore)
    store.dimension = 3
    store.collection_name = "chunks"
    store._collection = FakeCollection(insert_count)
    return store


def layer_store(insert_count=None):
    store = object.__new__(MilvusLayerStore)
    store.dimension = 3
    store.collection_name = "layers"
    store._collection = FakeCollection(insert_count)
    return store


def test_one_bad_chunk_vector_rejects_entire_batch_before_insert():
    store = chunk_store()
    batch = [
        (Chunk(text="one", chunk_id="one"), [1.0, 1.0, 1.0]),
        (Chunk(text="two", chunk_id="two"), [1.0, 1.0]),
    ]

    with pytest.raises(VectorWriteIncompleteError):
        store.insert_chunks(1, batch)

    assert store._collection.insert_calls == 0


def test_empty_chunk_batch_is_not_success():
    store = chunk_store()

    with pytest.raises(VectorWriteIncompleteError):
        store.insert_chunks(1, [])

    assert store._collection.insert_calls == 0


def test_chunk_insert_count_must_match():
    store = chunk_store(insert_count=0)

    with pytest.raises(VectorWriteIncompleteError):
        store.insert_chunks(1, [(Chunk(text="one", chunk_id="one"), [1, 1, 1])])


def test_layer_validation_happens_before_delete_or_insert():
    store = layer_store()

    with pytest.raises(VectorWriteIncompleteError):
        store.upsert_file_layers(1, [("l0", "summary", [1.0, 1.0])])

    assert store._collection.delete_calls == 0
    assert store._collection.insert_calls == 0


def test_layer_insert_count_must_match():
    store = layer_store(insert_count=1)
    layers = [
        ("l0", "summary", [1.0, 1.0, 1.0]),
        ("l1", "overview", [2.0, 2.0, 2.0]),
    ]

    with pytest.raises(VectorWriteIncompleteError):
        store.upsert_file_layers(1, layers)


def test_schema_v2_writes_require_and_include_workspace_id():
    chunks = chunk_store()
    chunks.expected_schema_version = 2
    layers = layer_store()
    layers.expected_schema_version = 2
    chunk_batch = [(Chunk(text="one", chunk_id="one"), [1.0, 1.0, 1.0])]
    layer_batch = [("l0", "summary", [1.0, 1.0, 1.0])]

    with pytest.raises(VectorWriteIncompleteError):
        chunks.insert_chunks(1, chunk_batch)
    with pytest.raises(VectorWriteIncompleteError):
        layers.upsert_file_layers(1, layer_batch)

    assert chunks._collection.insert_calls == 0
    assert layers._collection.delete_calls == 0
    chunks.insert_chunks(1, chunk_batch, workspace_id=9)
    layers.upsert_file_layers(1, layer_batch, workspace_id=9)
    assert chunks._collection.last_data[2] == [9]
    assert layers._collection.last_data[2] == [9]


@pytest.mark.parametrize(
    "module_name,store_class",
    [
        ("src.openrag.vectorstore.milvus_store", MilvusStore),
        ("src.openrag.vectorstore.milvus_layer_store", MilvusLayerStore),
    ],
)
def test_dimension_mismatch_never_drops_collection(
    monkeypatch, module_name, store_class
):
    module = __import__(module_name, fromlist=["unused"])
    existing = SimpleNamespace(
        schema=SimpleNamespace(
            fields=[
                SimpleNamespace(
                    name="embedding",
                    dtype=module.DataType.FLOAT_VECTOR,
                    params={"dim": 2},
                )
            ]
        ),
        indexes=[
            SimpleNamespace(field_name="embedding", params={"metric_type": "COSINE"})
        ],
        properties={},
        drop=lambda: pytest.fail("collection.drop must not be called"),
    )
    monkeypatch.setattr(module.utility, "has_collection", lambda *_, **__: True)
    monkeypatch.setattr(module, "Collection", lambda *_, **__: existing)
    store = object.__new__(store_class)
    store.collection_name = "existing"
    store.dimension = 3
    store._collection = None

    with pytest.raises(VectorSchemaMismatchError):
        store._connection_alias = "test"
        store.expected_schema_version = 1
        store.expected_embedding_fingerprint = None
        store._load_and_validate_collection()
