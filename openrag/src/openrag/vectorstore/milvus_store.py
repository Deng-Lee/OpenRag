"""Milvus vector store for chunk embeddings.

Environment variables:
    MILVUS_HOST  – Milvus server host (default: localhost)
    MILVUS_PORT  – Milvus gRPC port   (default: 19530)
"""

import logging
import os
from typing import Optional

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusException,
    connections,
    utility,
)

logger = logging.getLogger(__name__)

_COLLECTION = "openrag_chunks"
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
        host: str | None = None,
        port: str | int | None = None,
        collection_name: str = _COLLECTION,
        dimension: int = 1536,
    ):
        self.host = host or os.environ.get("MILVUS_HOST", "localhost")
        self.port = int(port or os.environ.get("MILVUS_PORT", "19530"))
        self.collection_name = collection_name
        self.dimension = dimension
        self._collection: Optional[Collection] = None

        self._connect()
        self._ensure_collection()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        alias = "default"
        try:
            connections.connect(alias=alias, host=self.host, port=self.port)
            logger.info("Connected to Milvus at %s:%s", self.host, self.port)
        except MilvusException as exc:
            logger.error("Failed to connect to Milvus: %s", exc)
            raise

    def _ensure_collection(self) -> None:
        if utility.has_collection(self.collection_name):
            existing = Collection(self.collection_name)
            existing_dim = None
            for field in existing.schema.fields:
                if field.dtype == DataType.FLOAT_VECTOR:
                    existing_dim = field.params.get("dim")
                    break
            if existing_dim and existing_dim != self.dimension:
                logger.warning(
                    "Collection '%s' has dim=%d but need dim=%d; dropping and recreating",
                    self.collection_name, existing_dim, self.dimension,
                )
                existing.drop()
            else:
                self._collection = existing
                self._collection.load()
                logger.info("Loaded existing collection '%s'", self.collection_name)
                return

        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="file_id", dtype=DataType.INT64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=_TEXT_MAX_LEN),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.dimension),
            FieldSchema(name="page", dtype=DataType.INT64),
            FieldSchema(name="level", dtype=DataType.INT64),
            FieldSchema(name="block_type", dtype=DataType.VARCHAR, max_length=32),
        ]
        schema = CollectionSchema(fields=fields, description="OpenRag document chunks")
        self._collection = Collection(name=self.collection_name, schema=schema)

        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        }
        self._collection.create_index(field_name="embedding", index_params=index_params)
        self._collection.load()
        logger.info("Created and loaded new collection '%s'", self.collection_name)

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------

    def insert_chunks(
        self,
        file_id: int,
        chunk_embeddings: list[tuple],
    ) -> int:
        """Insert chunk-embedding pairs into Milvus.

        Args:
            file_id: Source file database ID.
            chunk_embeddings: List of (Chunk, embedding) tuples from EmbeddingEngine.

        Returns:
            Number of inserted vectors.
        """
        if not chunk_embeddings:
            return 0

        chunk_ids = []
        file_ids = []
        texts = []
        embeddings = []
        pages = []
        levels = []
        block_types = []

        for chunk, emb in chunk_embeddings:
            if not isinstance(emb, list) or len(emb) != self.dimension:
                logger.warning(
                    "Skipping chunk with invalid embedding (type=%s, len=%s)",
                    type(emb).__name__,
                    len(emb) if isinstance(emb, list) else "N/A",
                )
                continue
            chunk_ids.append(str(getattr(chunk, "chunk_id", ""))[:64])
            file_ids.append(file_id)
            text = getattr(chunk, "text", str(chunk))
            texts.append(truncate_to_bytes(text, _TEXT_MAX_LEN))
            embeddings.append(emb)
            pages.append(getattr(chunk, "page", 0))
            levels.append(getattr(chunk, "level", 0))
            block_types.append(str(getattr(chunk, "block_type", "text"))[:32])

        if not chunk_ids:
            logger.warning("No valid embeddings to insert for file_id=%d", file_id)
            return 0

        data = [chunk_ids, file_ids, texts, embeddings, pages, levels, block_types]
        try:
            self._collection.insert(data)
            # Don't call flush() per insert — Milvus auto-flushes periodically.
            # Explicit flush blocks and can hang for hours on large datasets.
            logger.info("Inserted %d vectors for file_id=%d", len(chunk_ids), file_id)
        except MilvusException as exc:
            logger.error("Milvus insert failed for file_id=%d: %s", file_id, exc)
            raise
        return len(chunk_ids)

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
            output_fields = non_vector_output_field_names(self._collection.schema.fields)
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
            hits.append({
                "chunk_id": hit.entity.get("chunk_id"),
                "file_id": fid,
                "text": hit.entity.get("text"),
                "score": hit.score,
                "page": hit.entity.get("page", 0) if "page" in schema_names else 0,
                "level": hit.entity.get("level", 0) if "level" in schema_names else 0,
                "block_type": hit.entity.get("block_type", "text") if "block_type" in schema_names else "text",
            })
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
