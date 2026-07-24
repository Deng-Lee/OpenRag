"""Milvus collection for document L0 (abstract) / L1 (overview) embeddings."""

import logging
import math
import os
from uuid import uuid4
from numbers import Real
from typing import Optional

from pymilvus import (
    Collection,
    DataType,
    MilvusException,
    connections,
    utility,
)
from openrag.indexing.milvus_schema import read_collection_metadata

from openrag.vectorstore.milvus_store import (
    non_vector_output_field_names,
    truncate_to_bytes,
)
from .errors import (
    VectorCollectionUnavailableError,
    VectorSchemaMismatchError,
    VectorWriteIncompleteError,
)

logger = logging.getLogger(__name__)

_COLLECTION = "openrag_layers"
LEGACY_LAYER_COLLECTION = _COLLECTION
_TEXT_MAX_LEN = 65535
_LAYER_ID_MAX = 96
_MAX_FILE_IDS_IN_EXPR = 512


def _layer_row_id(file_id: int, layer: str) -> str:
    return f"{file_id}_{layer}"[:_LAYER_ID_MAX]


class MilvusLayerStore:
    """Stores one vector per (file_id, layer) for l0 / l1 text."""

    def __init__(
        self,
        collection_name: str,
        dimension: int,
        host: str | None = None,
        port: str | int | None = None,
        expected_schema_version: int = 1,
        expected_embedding_fingerprint: str | None = None,
        connection_role: str = "runtime",
        user: str | None = None,
        password: str | None = None,
        secure: bool = False,
        connection_timeout_seconds: int = 10,
    ):
        self.host = host or os.environ.get("MILVUS_HOST", "localhost")
        self.port = int(port or os.environ.get("MILVUS_PORT", "19530"))
        self.collection_name = collection_name
        self.dimension = dimension
        self.expected_schema_version = expected_schema_version
        self.expected_embedding_fingerprint = expected_embedding_fingerprint
        self.connection_role = connection_role
        self.user = user or os.environ.get("MILVUS_RUNTIME_USER")
        self.password = password or os.environ.get("MILVUS_RUNTIME_PASSWORD")
        self.secure = secure
        self.connection_timeout_seconds = connection_timeout_seconds
        self._connection_alias = f"openrag_runtime_{uuid4().hex}"
        self._collection: Optional[Collection] = None
        self._connect()
        self._load_and_validate_collection()

    def _connect(self) -> None:
        try:
            kwargs = {
                "alias": self._connection_alias,
                "host": self.host,
                "port": self.port,
                "secure": self.secure,
                "timeout": self.connection_timeout_seconds,
            }
            if self.user:
                kwargs.update(user=self.user, password=self.password or "")
            connections.connect(**kwargs)
        except MilvusException as exc:
            logger.error("MilvusLayerStore connect failed: %s", exc)
            raise

    def _load_and_validate_collection(self) -> None:
        if not utility.has_collection(
            self.collection_name, using=self._connection_alias
        ):
            raise VectorCollectionUnavailableError(
                f"Runtime Collection {self.collection_name!r} does not exist"
            )
        self._collection = Collection(
            self.collection_name, using=self._connection_alias
        )
        self.validate_manifest()
        self._collection.load()

    def describe_schema(self) -> dict:
        fields = {field.name: field for field in self._collection.schema.fields}
        indexes = [
            dict(index.params or {})
            for index in self._collection.indexes
            if index.field_name == "embedding"
        ]
        return {
            "fields": fields,
            "indexes": indexes,
            "metadata": read_collection_metadata(self._collection),
        }

    def validate_manifest(self) -> None:
        description = self.describe_schema()
        fields = description["fields"]
        required = {"layer_row_id", "file_id", "layer", "text", "embedding"}
        if self.expected_schema_version >= 2:
            required.add("workspace_id")
        vector = fields.get("embedding")
        metrics = {
            str(index.get("metric_type", "")).upper()
            for index in description["indexes"]
        }
        fingerprint = str(description["metadata"].get("embedding_fingerprint", ""))
        observed_version = str(description["metadata"].get("schema_version", ""))
        types_valid = all(
            fields.get(name) is not None and fields[name].dtype == dtype
            for name, dtype in {
                "file_id": DataType.INT64,
                "text": DataType.VARCHAR,
                "embedding": DataType.FLOAT_VECTOR,
            }.items()
        )
        if (
            not required.issubset(fields)
            or vector is None
            or vector.dtype != DataType.FLOAT_VECTOR
            or not types_valid
            or int(vector.params.get("dim") or 0) != self.dimension
            or "COSINE" not in metrics
            or (
                observed_version
                and observed_version != str(self.expected_schema_version)
            )
            or (
                self.expected_embedding_fingerprint
                and fingerprint != self.expected_embedding_fingerprint
            )
        ):
            raise VectorSchemaMismatchError(
                f"Collection {self.collection_name!r} does not match runtime manifest"
            )

    def delete_by_file_id(self, file_id: int) -> None:
        expr = f"file_id == {file_id}"
        self._collection.delete(expr)
        logger.debug("Deleted layer vectors for file_id=%s", file_id)

    def upsert_file_layers(
        self,
        file_id: int,
        layer_embeddings: list[tuple[str, str, list[float]]],
        workspace_id: int | None = None,
    ) -> int:
        """Replace a file's already-generated and validated L0/L1 vectors."""
        self._validate_layer_embeddings(layer_embeddings)
        layer_ids: list[str] = []
        file_ids: list[int] = []
        layers: list[str] = []
        texts: list[str] = []
        embeddings: list[list[float]] = []

        for layer, text, emb in layer_embeddings:
            t = text.strip()
            layer_ids.append(_layer_row_id(file_id, layer))
            file_ids.append(file_id)
            layers.append(layer)
            texts.append(truncate_to_bytes(t, _TEXT_MAX_LEN))
            embeddings.append(list(emb))

        schema_version = getattr(self, "expected_schema_version", 1)
        if schema_version >= 2 and workspace_id is None:
            raise VectorWriteIncompleteError(
                "Schema v2 layer writes require workspace_id"
            )
        self.delete_by_file_id(file_id)
        data = [layer_ids, file_ids]
        if schema_version >= 2:
            data.append([int(workspace_id)] * len(layer_ids))
        data.extend([layers, texts, embeddings])
        mutation_result = self._collection.insert(data)
        self._assert_insert_count(len(layer_ids), mutation_result)
        logger.info("Inserted %d layer rows for file_id=%s", len(layer_ids), file_id)
        return len(layer_ids)

    def _validate_layer_embeddings(
        self, layer_embeddings: list[tuple[str, str, list[float]]]
    ) -> None:
        if not layer_embeddings:
            raise VectorWriteIncompleteError("Layer vector batch must not be empty")
        seen_layers: set[str] = set()
        for item in layer_embeddings:
            if not isinstance(item, tuple) or len(item) != 3:
                raise VectorWriteIncompleteError("Layer vector batch is malformed")
            layer, text, embedding = item
            if layer not in {"l0", "l1"} or layer in seen_layers:
                raise VectorWriteIncompleteError(
                    "Layer vector names must be unique l0/l1 values"
                )
            seen_layers.add(layer)
            if not isinstance(text, str) or not text.strip():
                raise VectorWriteIncompleteError("Layer vector text must not be blank")
            if (
                not isinstance(embedding, (list, tuple))
                or len(embedding) != self.dimension
            ):
                raise VectorWriteIncompleteError("Layer vector dimension is invalid")
            values = []
            for value in embedding:
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise VectorWriteIncompleteError(
                        "Layer vector contains invalid values"
                    )
                number = float(value)
                if not math.isfinite(number):
                    raise VectorWriteIncompleteError(
                        "Layer vector contains invalid values"
                    )
                values.append(number)
            if math.sqrt(sum(value * value for value in values)) <= 1e-12:
                raise VectorWriteIncompleteError("Layer vector must not be zero")

    @staticmethod
    def _assert_insert_count(expected: int, mutation_result) -> None:
        actual = getattr(mutation_result, "insert_count", None)
        if actual != expected:
            raise VectorWriteIncompleteError(
                f"Milvus inserted {actual!r} layer vectors; expected {expected}",
                retryable=True,
            )

    def probe(self) -> None:
        if not utility.has_collection(
            self.collection_name, using=self._connection_alias
        ):
            raise RuntimeError("Milvus layer collection is unavailable")
        self.validate_manifest()
        self._collection.load()

    def search_layers(
        self,
        query_embedding: list[float],
        layer: str,
        top_k: int,
        file_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """ANN on one layer (l0 or l1). Optionally restrict to file_ids."""
        vec = [float(x) for x in query_embedding]
        clean_ids: list[int] = []
        if file_ids:
            for i in file_ids:
                try:
                    clean_ids.append(int(i))
                except (TypeError, ValueError):
                    continue

        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
        expr = f"layer == '{layer}'"
        post_filter: set[int] | None = None
        limit = top_k
        if clean_ids:
            if len(clean_ids) <= _MAX_FILE_IDS_IN_EXPR:
                id_list = ",".join(str(i) for i in clean_ids)
                expr = f"{expr} && file_id in [{id_list}]"
            else:
                post_filter = set(clean_ids)
                limit = min(max(top_k * 50, 500), 4096)

        wanted = ["layer_row_id", "file_id", "layer", "text"]
        schema_names = {f.name for f in self._collection.schema.fields}
        output_fields = [f for f in wanted if f in schema_names]
        if not output_fields:
            output_fields = non_vector_output_field_names(
                self._collection.schema.fields
            )
        if not output_fields:
            raise RuntimeError(
                f"Milvus layer collection {self.collection_name!r} has no scalar fields for search "
                f"(field names present: {sorted(schema_names)})."
            )

        results = self._collection.search(
            data=[vec],
            anns_field="embedding",
            param=search_params,
            limit=limit,
            expr=expr,
            output_fields=output_fields,
        )
        hits: list[dict] = []
        for hit in results[0]:
            fid = hit.entity.get("file_id")
            if post_filter is not None:
                try:
                    if int(fid) not in post_filter:
                        continue
                except (TypeError, ValueError):
                    continue
            hits.append(
                {
                    "layer_row_id": hit.entity.get("layer_row_id"),
                    "file_id": fid,
                    "layer": hit.entity.get("layer"),
                    "text": hit.entity.get("text"),
                    "score": float(hit.score),
                }
            )
            if post_filter is not None and len(hits) >= top_k:
                break
        return hits
