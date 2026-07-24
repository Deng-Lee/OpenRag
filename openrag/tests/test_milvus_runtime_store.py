"""Runtime Milvus stores only load and validate existing resources."""

from types import SimpleNamespace

import pytest

from openrag.config import VectorDBConfig
from openrag.vectorstore.errors import (
    VectorCollectionUnavailableError,
    VectorSchemaMismatchError,
)
from openrag.vectorstore.milvus_layer_store import MilvusLayerStore
from openrag.vectorstore.milvus_store import MilvusStore


def test_vector_db_roles_use_explicit_milvus_environment(monkeypatch):
    monkeypatch.setenv("MILVUS_RUNTIME_USER", "runtime")
    monkeypatch.setenv("MILVUS_RUNTIME_PASSWORD", "runtime-secret")
    monkeypatch.setenv("MILVUS_INDEX_ADMIN_USER", "index-admin")

    config = VectorDBConfig()

    assert config.runtime_user == "runtime"
    assert config.runtime_password.get_secret_value() == "runtime-secret"
    assert config.admin_user == "index-admin"


@pytest.mark.parametrize(
    ("module_name", "store_class", "collection_name"),
    [
        ("openrag.vectorstore.milvus_store", MilvusStore, "missing_chunks"),
        ("openrag.vectorstore.milvus_layer_store", MilvusLayerStore, "missing_layers"),
    ],
)
def test_missing_collection_fails_without_creating(
    monkeypatch, module_name, store_class, collection_name
):
    module = __import__(module_name, fromlist=["unused"])
    monkeypatch.setattr(module.connections, "connect", lambda **_: None)
    monkeypatch.setattr(module.utility, "has_collection", lambda *_, **__: False)
    monkeypatch.setattr(
        module,
        "Collection",
        lambda *_, **__: pytest.fail("runtime must not construct a missing Collection"),
    )

    with pytest.raises(VectorCollectionUnavailableError):
        store_class(collection_name=collection_name, dimension=3)


@pytest.mark.parametrize(
    ("module_name", "store_class", "collection_name"),
    [
        ("openrag.vectorstore.milvus_store", MilvusStore, "chunks"),
        ("openrag.vectorstore.milvus_layer_store", MilvusLayerStore, "layers"),
    ],
)
def test_wrong_metric_fails_closed(
    monkeypatch, module_name, store_class, collection_name
):
    module = __import__(module_name, fromlist=["unused"])
    fields = [
        SimpleNamespace(
            name="embedding", dtype=module.DataType.FLOAT_VECTOR, params={"dim": 3}
        )
    ]
    collection = SimpleNamespace(
        schema=SimpleNamespace(fields=fields, description=""),
        indexes=[SimpleNamespace(field_name="embedding", params={"metric_type": "L2"})],
        properties={},
        load=lambda: None,
    )
    monkeypatch.setattr(module.connections, "connect", lambda **_: None)
    monkeypatch.setattr(module.utility, "has_collection", lambda *_, **__: True)
    monkeypatch.setattr(module, "Collection", lambda *_, **__: collection)

    with pytest.raises(VectorSchemaMismatchError):
        store_class(collection_name=collection_name, dimension=3)
