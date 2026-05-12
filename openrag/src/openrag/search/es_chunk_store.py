"""Elasticsearch: per-workspace chunk full-text index."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CHUNK_MAPPINGS: dict[str, Any] = {
    "properties": {
        "chunk_id": {"type": "keyword"},
        "file_id": {"type": "long"},
        "workspace_id": {"type": "long"},
        "workspace_slug": {"type": "keyword"},
        "content": {"type": "text"},
    }
}


class EsChunkStore:
    """Upsert/delete chunk docs; search BM25 scores for a bounded chunk set."""

    def __init__(
        self,
        hosts: list[str],
        *,
        request_timeout: int = 30,
        verify_certs: bool = True,
    ):
        from elasticsearch import Elasticsearch

        self._client = Elasticsearch(
            hosts,
            request_timeout=request_timeout,
            verify_certs=verify_certs,
        )

    @property
    def client(self):
        return self._client

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def ensure_index(self, index_name: str) -> None:
        if self._client.indices.exists(index=index_name):
            return
        self._client.indices.create(
            index=index_name,
            settings={"number_of_shards": 1, "number_of_replicas": 0},
            mappings=_CHUNK_MAPPINGS,
        )
        logger.info("Created Elasticsearch index %s", index_name)

    def bulk_upsert_chunks(self, index_name: str, docs: list[dict[str, Any]]) -> int:
        if not docs:
            return 0
        from elasticsearch.helpers import bulk

        actions = []
        for d in docs:
            cid = d.get("chunk_id")
            if not cid:
                continue
            actions.append(
                {
                    "_op_type": "index",
                    "_index": index_name,
                    "_id": str(cid),
                    "_source": d,
                }
            )
        if not actions:
            return 0
        ok, errors = bulk(self._client, actions, refresh="wait_for", raise_on_error=False)
        if errors:
            logger.warning("Elasticsearch bulk had errors: %s", errors[:3])
        return int(ok)

    def delete_by_file_id(self, index_name: str, file_id: int) -> None:
        try:
            if not self._client.indices.exists(index=index_name):
                return
        except Exception:
            return
        self._client.delete_by_query(
            index=index_name,
            query={"term": {"file_id": file_id}},
            refresh=True,
            conflicts="proceed",
        )

    def search_chunk_scores(
        self,
        *,
        index_names: list[str],
        query_text: str,
        file_ids: list[int],
        chunk_ids: list[str],
    ) -> dict[str, float]:
        """BM25 scores for chunk_ids restricted to file_ids; missing chunks omitted."""
        if not index_names or not chunk_ids or not file_ids:
            return {}
        index_arg = ",".join(index_names)
        size = min(len(chunk_ids), 500)
        q = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query_text,
                            "type": "best_fields",
                            "fields": ["content"],
                        }
                    },
                    {"ids": {"values": chunk_ids}},
                ],
                "filter": [{"terms": {"file_id": file_ids}}],
            }
        }
        resp = self._client.search(
            index=index_arg,
            size=size,
            query=q,
            _source=False,
        )
        out: dict[str, float] = {}
        for hit in resp.get("hits", {}).get("hits", []):
            cid = hit.get("_id")
            if cid is not None:
                out[str(cid)] = float(hit.get("_score") or 0.0)
        return out


def create_es_chunk_store_from_config() -> Optional[EsChunkStore]:
    """Return store if enabled and reachable; otherwise None."""
    try:
        from openrag.config import get_config
    except Exception:
        return None
    cfg = get_config().elasticsearch
    if not cfg.enabled:
        return None
    hosts = [h.strip() for h in cfg.hosts.split(",") if h.strip()]
    if not hosts:
        logger.warning("Elasticsearch enabled but ELASTICSEARCH__HOSTS empty")
        return None
    try:
        store = EsChunkStore(
            hosts,
            request_timeout=cfg.request_timeout,
            verify_certs=cfg.verify_certs,
        )
        if not store.ping():
            logger.warning("Elasticsearch ping failed for hosts=%s", hosts)
            return None
        return store
    except Exception as exc:
        logger.warning("Elasticsearch client init failed: %s", exc)
        return None
