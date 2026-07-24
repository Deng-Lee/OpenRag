"""Candidate Collection provisioning is explicit and idempotent."""

import pytest

from openrag.indexing.milvus_provisioner import (
    MilvusCollectionProvisioner,
    ProvisioningConflictError,
)


class FakeAdminBackend:
    def __init__(self):
        self.collections = {}
        self.create_calls = []
        self.drop_calls = []

    def has_collection(self, name):
        return name in self.collections

    def create_collection(self, name, schema, metadata):
        self.create_calls.append(name)
        self.collections[name] = {
            "schema": schema.to_dict(),
            "metadata": dict(metadata),
            "indexes": [],
            "loaded": False,
        }

    def create_index(self, name, field_name, index_params):
        self.collections[name]["indexes"].append(
            {"field_name": field_name, **index_params}
        )

    def load_collection(self, name):
        self.collections[name]["loaded"] = True

    def describe_collection(self, name):
        return self.collections[name]


def manifest(suffix="a"):
    return {
        "generation_id": f"generation-{suffix}",
        "schema_version": 1,
        "embedding_dimension": 3,
        "embedding_fingerprint": suffix * 64,
        "chunk_collection_name": f"chunks_{suffix}",
        "layer_collection_name": f"layers_{suffix}",
        "source_generation_id": None,
    }


def test_provisioner_creates_both_collections_and_is_idempotent():
    backend = FakeAdminBackend()
    provisioner = MilvusCollectionProvisioner(backend)

    first = provisioner.provision_generation(manifest())
    second = provisioner.provision_generation(manifest())

    assert first == second
    assert backend.create_calls == ["chunks_a", "layers_a"]
    assert backend.collections["chunks_a"]["loaded"] is True
    assert (
        backend.collections["layers_a"]["metadata"]["embedding_fingerprint"] == "a" * 64
    )


def test_provisioner_rejects_unknown_same_name_resource():
    backend = FakeAdminBackend()
    backend.collections["chunks_a"] = {
        "schema": {},
        "metadata": {},
        "indexes": [],
        "loaded": False,
    }

    with pytest.raises(ProvisioningConflictError):
        MilvusCollectionProvisioner(backend).provision_generation(manifest())

    assert backend.create_calls == []


def test_partial_provision_failure_never_drops_created_resource():
    backend = FakeAdminBackend()
    original = backend.create_collection

    def fail_layer(name, schema, metadata):
        if name.startswith("layers"):
            raise RuntimeError("injected layer failure")
        original(name, schema, metadata)

    backend.create_collection = fail_layer

    with pytest.raises(RuntimeError):
        MilvusCollectionProvisioner(backend).provision_generation(manifest())

    assert "chunks_a" in backend.collections
    assert backend.drop_calls == []
