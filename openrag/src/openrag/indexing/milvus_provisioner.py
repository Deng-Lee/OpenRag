"""Explicit control-plane creation of candidate Milvus Collections."""

import json
from typing import Any, Protocol
from pymilvus import Collection, connections, utility
from openrag.indexing.milvus_schema import (
    build_collection_schema,
    chunk_schema_spec,
    describe_collection,
    layer_schema_spec,
)


class ProvisioningConflictError(RuntimeError):
    pass


class AdminBackend(Protocol):
    def has_collection(self, name: str) -> bool: ...
    def create_collection(self, name, schema, metadata) -> None: ...
    def create_index(self, name, field_name, index_params) -> None: ...
    def load_collection(self, name) -> None: ...
    def describe_collection(self, name) -> dict[str, Any]: ...


class PyMilvusAdminBackend:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str | None = None,
        password: str | None = None,
    ):
        self.alias = "openrag_index_admin"
        kwargs = {"alias": self.alias, "host": host, "port": port}
        if user:
            kwargs.update(user=user, password=password or "")
        connections.connect(**kwargs)

    def has_collection(self, name):
        return utility.has_collection(name, using=self.alias)

    def create_collection(self, name, schema, metadata):
        description = "openrag:" + json.dumps(
            metadata, sort_keys=True, separators=(",", ":")
        )
        collection = Collection(
            name=name,
            schema=build_collection_schema(schema, description),
            using=self.alias,
        )
        collection.set_properties(metadata)

    def create_index(self, name, field_name, index_params):
        Collection(name, using=self.alias).create_index(field_name, index_params)

    def load_collection(self, name):
        Collection(name, using=self.alias).load()

    def describe_collection(self, name):
        return describe_collection(Collection(name, using=self.alias))


class MilvusCollectionProvisioner:
    def __init__(self, backend: AdminBackend):
        self.backend = backend

    def provision_generation(self, manifest: dict[str, Any]) -> dict[str, str]:
        dimension, version = int(manifest["embedding_dimension"]), int(
            manifest["schema_version"]
        )
        common = {
            "generation_id": str(manifest["generation_id"]),
            "embedding_fingerprint": str(manifest["embedding_fingerprint"]),
            "schema_version": str(version),
            "source_generation_id": str(manifest.get("source_generation_id") or ""),
        }
        resources = (
            (
                manifest["chunk_collection_name"],
                chunk_schema_spec(dimension, version),
                "chunks",
            ),
            (
                manifest["layer_collection_name"],
                layer_schema_spec(dimension, version),
                "layers",
            ),
        )
        for name, schema, role in resources:
            metadata = {**common, "collection_role": role}
            if self.backend.has_collection(name):
                self._assert_matches(name, schema.to_dict(), metadata)
                continue
            self.backend.create_collection(name, schema, metadata)
            self.backend.create_index(name, "embedding", schema.index_params)
            self.backend.load_collection(name)
            self._assert_matches(name, schema.to_dict(), metadata)
        return {"chunks": resources[0][0], "layers": resources[1][0]}

    def _assert_matches(self, name, schema, metadata):
        observed = self.backend.describe_collection(name)
        schema_matches = (
            observed.get("schema") == schema
            or observed.get("fields") == schema["fields"]
        )
        metadata_matches = all(
            str(observed.get("metadata", {}).get(k, "")) == str(v)
            for k, v in metadata.items()
        )
        vector_indexes = [
            index
            for index in observed.get("indexes", [])
            if index.get("field_name") == "embedding"
        ]
        index_matches = any(
            str(index.get("metric_type", "")).upper()
            == schema["index_params"]["metric_type"]
            and index.get("index_type") == schema["index_params"]["index_type"]
            for index in vector_indexes
        )
        if not schema_matches or not metadata_matches or not index_matches:
            raise ProvisioningConflictError(
                f"Collection {name!r} exists but is not owned by this generation"
            )
