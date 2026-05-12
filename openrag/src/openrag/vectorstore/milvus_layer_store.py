"""Milvus collection for document L0 (abstract) / L1 (overview) embeddings."""

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

from openrag.vectorstore.milvus_store import non_vector_output_field_names

logger = logging.getLogger(__name__)

_COLLECTION = "openrag_layers"
_TEXT_MAX_LEN = 65535
_LAYER_ID_MAX = 96
_MAX_FILE_IDS_IN_EXPR = 512


def _layer_row_id(file_id: int, layer: str) -> str:
    return f"{file_id}_{layer}"[:_LAYER_ID_MAX]


class MilvusLayerStore:
    """Stores one vector per (file_id, layer) for l0 / l1 text."""

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

    def _connect(self) -> None:
        try:
            connections.connect(alias="default", host=self.host, port=self.port)
        except MilvusException as exc:
            logger.error("MilvusLayerStore connect failed: %s", exc)
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
                    "Dropping layer collection '%s' (dim %s != %s)",
                    self.collection_name,
                    existing_dim,
                    self.dimension,
                )
                existing.drop()
            else:
                self._collection = existing
                self._collection.load()
                logger.info("Loaded Milvus collection '%s'", self.collection_name)
                return

        fields = [
            FieldSchema(
                name="layer_row_id",
                dtype=DataType.VARCHAR,
                is_primary=True,
                max_length=_LAYER_ID_MAX,
            ),
            FieldSchema(name="file_id", dtype=DataType.INT64),
            FieldSchema(name="layer", dtype=DataType.VARCHAR, max_length=8),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=_TEXT_MAX_LEN),
            FieldSchema(
                name="embedding",
                dtype=DataType.FLOAT_VECTOR,
                dim=self.dimension,
            ),
        ]
        schema = CollectionSchema(
            fields=fields,
            description="OpenRag L0/L1 layer embeddings",
        )
        self._collection = Collection(name=self.collection_name, schema=schema)
        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        }
        self._collection.create_index(field_name="embedding", index_params=index_params)
        self._collection.load()
        logger.info("Created Milvus collection '%s'", self.collection_name)

    def delete_by_file_id(self, file_id: int) -> None:
        expr = f"file_id == {file_id}"
        self._collection.delete(expr)
        logger.debug("Deleted layer vectors for file_id=%s", file_id)

    def upsert_file_layers(
        self,
        file_id: int,
        l0_text: Optional[str],
        l1_text: Optional[str],
        embed_text: callable,
    ) -> int:
        """Replace L0/L1 rows for a file. embed_text: str -> list[float]. Returns rows written."""
        self.delete_by_file_id(file_id)
        rows = 0
        layer_ids: list[str] = []
        file_ids: list[int] = []
        layers: list[str] = []
        texts: list[str] = []
        embeddings: list[list[float]] = []

        for layer, text in (("l0", l0_text or ""), ("l1", l1_text or "")):
            t = (text or "").strip()
            if not t:
                continue
            emb = embed_text(t)
            if not isinstance(emb, list) or len(emb) != self.dimension:
                logger.warning(
                    "Skip layer %s for file_id=%s: bad embedding len=%s",
                    layer,
                    file_id,
                    len(emb) if isinstance(emb, list) else None,
                )
                continue
            layer_ids.append(_layer_row_id(file_id, layer))
            file_ids.append(file_id)
            layers.append(layer)
            texts.append(t[:_TEXT_MAX_LEN])
            embeddings.append(emb)
            rows += 1

        if rows:
            data = [layer_ids, file_ids, layers, texts, embeddings]
            self._collection.insert(data)
            logger.info("Inserted %d layer rows for file_id=%s", rows, file_id)
        return rows

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
            output_fields = non_vector_output_field_names(self._collection.schema.fields)
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
