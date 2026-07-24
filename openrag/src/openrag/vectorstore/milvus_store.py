"""Milvus vector store for chunk embeddings.

Environment variables:
    MILVUS_HOST  – Milvus server host (default: localhost)
    MILVUS_PORT  – Milvus gRPC port   (default: 19530)
"""

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

logger = logging.getLogger(__name__)

from .errors import (
    VectorCollectionUnavailableError,
    VectorSchemaMismatchError,
    VectorWriteIncompleteError,
)

_COLLECTION = "openrag_chunks"
LEGACY_CHUNK_COLLECTION = _COLLECTION
_TEXT_MAX_LEN = 65535
# Milvus 布尔表达式过长或边界情况可能触发服务端异常；超过则不在 expr 里过滤，改为内存过滤
_MAX_FILE_IDS_IN_EXPR = 512


def truncate_to_bytes(text: str, max_bytes: int = _TEXT_MAX_LEN) -> str:
    """Truncate ``text`` so its UTF-8 encoding fits within ``max_bytes``.

    Milvus enforces VARCHAR ``max_length`` in UTF-8 *bytes*, not characters.
    A character-based slice (``text[:max_bytes]``) overflows for any non-ASCII
    content, so encode, slice on the byte boundary, then drop any partial
    trailing multibyte sequence via ``errors="ignore"``.
    """
    if text is None:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def non_vector_output_field_names(schema_fields: list) -> list[str]:
    """Scalar field names safe for Milvus ``search(..., output_fields=...)``.

    If ``output_fields`` is omitted (``None``), some Milvus versions try to return
    all columns including the vector field and respond with
    ``Unsupported field type: 0`` (``DataType.NONE``).
    """
    vector_dtypes = {DataType.BINARY_VECTOR, DataType.FLOAT_VECTOR}
    for _name in (
        "FLOAT16_VECTOR",
        "BFLOAT16_VECTOR",
        "SPARSE_FLOAT_VECTOR",
        "INT8_VECTOR",
        "_ARRAY_OF_VECTOR",
    ):
        _dt = getattr(DataType, _name, None)
        if _dt is not None:
            vector_dtypes.add(_dt)
    return [f.name for f in schema_fields if f.dtype not in vector_dtypes]


class MilvusStore:
    """Manages a single Milvus collection for document chunk vectors."""

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

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

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
            logger.info("Connected to Milvus at %s:%s", self.host, self.port)
        except MilvusException as exc:
            logger.error("Failed to connect to Milvus: %s", exc)
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
        required = {
            "chunk_id",
            "file_id",
            "text",
            "embedding",
            "page",
            "level",
            "block_type",
        }
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

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------

    def insert_chunks(
        self,
        file_id: int,
        chunk_embeddings: list[tuple],
        workspace_id: int | None = None,
    ) -> int:
        """Insert chunk-embedding pairs into Milvus.

        Args:
            file_id: Source file database ID.
            chunk_embeddings: List of (Chunk, embedding) tuples from EmbeddingEngine.

        Returns:
            Number of inserted vectors.
        """
        self._validate_chunk_embeddings(chunk_embeddings)

        chunk_ids = []
        file_ids = []
        texts = []
        embeddings = []
        pages = []
        levels = []
        block_types = []

        for chunk, emb in chunk_embeddings:
            chunk_ids.append(str(getattr(chunk, "chunk_id", ""))[:64])
            file_ids.append(file_id)
            text = getattr(chunk, "text", str(chunk))
            texts.append(truncate_to_bytes(text, _TEXT_MAX_LEN))
            embeddings.append(emb)
            pages.append(getattr(chunk, "page", 0))
            levels.append(getattr(chunk, "level", 0))
            block_types.append(str(getattr(chunk, "block_type", "text"))[:32])

        data = [chunk_ids, file_ids]
        if getattr(self, "expected_schema_version", 1) >= 2:
            if workspace_id is None:
                raise VectorWriteIncompleteError(
                    "Schema v2 chunk writes require workspace_id"
                )
            data.append([int(workspace_id)] * len(chunk_ids))
        data.extend([texts, embeddings, pages, levels, block_types])
        try:
            mutation_result = self._collection.insert(data)
            self._assert_insert_count(len(chunk_ids), mutation_result)
            # Don't call flush() per insert — Milvus auto-flushes periodically.
            # Explicit flush blocks and can hang for hours on large datasets.
            logger.info("Inserted %d vectors for file_id=%d", len(chunk_ids), file_id)
        except MilvusException as exc:
            logger.error("Milvus insert failed for file_id=%d: %s", file_id, exc)
            raise
        return len(chunk_ids)

    def _validate_chunk_embeddings(self, chunk_embeddings: list[tuple]) -> None:
        if not chunk_embeddings:
            raise VectorWriteIncompleteError("Chunk vector batch must not be empty")
        chunk_ids: list[str] = []
        for pair in chunk_embeddings:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise VectorWriteIncompleteError("Chunk vector batch is malformed")
            chunk, embedding = pair
            chunk_id = str(getattr(chunk, "chunk_id", "") or "").strip()
            if not chunk_id:
                raise VectorWriteIncompleteError(
                    "Every chunk vector must have a chunk ID"
                )
            chunk_ids.append(chunk_id[:64])
            if (
                not isinstance(embedding, (list, tuple))
                or len(embedding) != self.dimension
            ):
                raise VectorWriteIncompleteError("Chunk vector dimension is invalid")
            values = []
            for value in embedding:
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise VectorWriteIncompleteError(
                        "Chunk vector contains invalid values"
                    )
                number = float(value)
                if not math.isfinite(number):
                    raise VectorWriteIncompleteError(
                        "Chunk vector contains invalid values"
                    )
                values.append(number)
            if math.sqrt(sum(value * value for value in values)) <= 1e-12:
                raise VectorWriteIncompleteError("Chunk vector must not be zero")
        if len(set(chunk_ids)) != len(chunk_ids):
            raise VectorWriteIncompleteError("Chunk vector IDs must be unique")

    @staticmethod
    def _assert_insert_count(expected: int, mutation_result) -> None:
        actual = getattr(mutation_result, "insert_count", None)
        if actual != expected:
            raise VectorWriteIncompleteError(
                f"Milvus inserted {actual!r} chunk vectors; expected {expected}",
                retryable=True,
            )

    def probe(self) -> None:
        if not utility.has_collection(
            self.collection_name, using=self._connection_alias
        ):
            raise RuntimeError("Milvus chunk collection is unavailable")
        self.validate_manifest()
        self._collection.load()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        file_ids: list[int] | None = None,
    ) -> list[dict]:
        """ANN search against stored chunk vectors.

        Args:
            query_embedding: Query vector.
            top_k: Number of results.
            file_ids: Optional filter to restrict search to specific files.

        Returns:
            List of dicts with keys: chunk_id, file_id, text, score, page, level, block_type.
        """
        vec = [float(x) for x in query_embedding]
        clean_ids: list[int] | None = None
        if file_ids is not None:
            clean_ids = []
            for fid in file_ids:
                try:
                    clean_ids.append(int(fid))
                except (TypeError, ValueError):
                    continue
            if not clean_ids:
                clean_ids = None

        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
        expr: str | None = None
        post_filter: set[int] | None = None
        limit = top_k
        if clean_ids is not None:
            if len(clean_ids) <= _MAX_FILE_IDS_IN_EXPR:
                id_list = ",".join(str(fid) for fid in clean_ids)
                expr = f"file_id in [{id_list}]"
            else:
                # 避免超长 in 表达式导致 Milvus 异常；多取候选再在内存中过滤
                post_filter = set(clean_ids)
                limit = min(max(top_k * 50, 500), 4096)

        # page/level/block_type 由 RetrievalService._enrich_hits 从 DB 补全，不必向 Milvus 索取，
        # 减轻 payload，并避免部分 2.3 服务端与新版 SDK 组合下标量列解析异常。
        wanted_outputs = ["chunk_id", "file_id", "text"]
        schema_names = {f.name for f in self._collection.schema.fields}
        output_fields = [f for f in wanted_outputs if f in schema_names]
        if not output_fields:
            output_fields = non_vector_output_field_names(
                self._collection.schema.fields
            )
        if not output_fields:
            raise RuntimeError(
                f"Milvus collection {self.collection_name!r} has no scalar fields for search "
                f"(field names present: {sorted(schema_names)}). "
                "Drop or rename the collection so OpenRag can recreate the expected schema."
            )

        results = self._collection.search(
            data=[vec],
            anns_field="embedding",
            param=search_params,
            limit=limit,
            expr=expr,
            output_fields=output_fields,
        )

        hits = []
        for hit in results[0]:
            fid = hit.entity.get("file_id")
            if post_filter is not None and expr is None:
                try:
                    if int(fid) not in post_filter:
                        continue
                except (TypeError, ValueError):
                    continue
            hits.append(
                {
                    "chunk_id": hit.entity.get("chunk_id"),
                    "file_id": fid,
                    "text": hit.entity.get("text"),
                    "score": hit.score,
                    "page": hit.entity.get("page", 0) if "page" in schema_names else 0,
                    "level": (
                        hit.entity.get("level", 0) if "level" in schema_names else 0
                    ),
                    "block_type": (
                        hit.entity.get("block_type", "text")
                        if "block_type" in schema_names
                        else "text"
                    ),
                }
            )
            if post_filter is not None and expr is None and len(hits) >= top_k:
                break
        return hits

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_by_file_id(self, file_id: int) -> None:
        """Delete all vectors belonging to a file."""
        expr = f"file_id == {file_id}"
        self._collection.delete(expr)
        self._collection.flush()
        logger.info("Deleted vectors for file_id=%d", file_id)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return total number of entities in the collection."""
        self._collection.flush()
        return self._collection.num_entities
