"""Elasticsearch: per-workspace chunk full-text index."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Optional

from openrag.search.es_chunk_contract import (
    build_chunk_mapping,
    extract_exact_terms,
    validate_chunk_search_document,
)

logger = logging.getLogger(__name__)

_FILE_SCOPE_BATCH_SIZE = 1000

_LEGACY_CHUNK_MAPPINGS: dict[str, Any] = {
    "properties": {
        "chunk_id": {"type": "keyword"},
        "file_id": {"type": "long"},
        "workspace_id": {"type": "long"},
        "workspace_slug": {"type": "keyword"},
        "content": {"type": "text"},
    }
}


class ElasticsearchRequiredError(RuntimeError):
    """Raised when Elasticsearch is mandatory but unavailable."""


class WorkspaceAliasConflictError(RuntimeError):
    """Raised when workspace aliases point somewhere unexpected."""


@dataclass(frozen=True)
class AliasEnsureResult:
    changed: bool
    previous_state: dict[str, dict[str, dict[str, Any]]]
    final_state: dict[str, dict[str, dict[str, Any]]]


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
            mappings=_LEGACY_CHUNK_MAPPINGS,
        )
        logger.info("Created Elasticsearch index %s", index_name)

    def validate_index_contract(self, index_name: str) -> None:
        response = self._client.indices.get_mapping(index=index_name)
        actual = (response.get(index_name) or {}).get("mappings")
        expected = build_chunk_mapping()
        if _normalize_mapping_from_es(actual) != expected:
            raise RuntimeError(
                f"Elasticsearch index contract mismatch for {index_name}"
            )

    def ensure_versioned_index(self, index_name: str) -> bool:
        if self._client.indices.exists(index=index_name):
            self.validate_index_contract(index_name)
            return False
        self._client.indices.create(
            index=index_name,
            settings={"number_of_shards": 1, "number_of_replicas": 0},
            mappings=build_chunk_mapping(),
        )
        logger.info("Created versioned Elasticsearch index %s", index_name)
        return True

    def get_alias_state(
        self, aliases: list[str]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        state: dict[str, dict[str, dict[str, Any]]] = {}
        for alias in aliases:
            if not self._client.indices.exists_alias(name=alias):
                state[alias] = {}
                continue
            response = self._client.indices.get_alias(name=alias)
            state[alias] = {
                str(index_name): dict(
                    (index_data.get("aliases") or {}).get(alias) or {}
                )
                for index_name, index_data in response.items()
            }
        return state

    def update_workspace_aliases(
        self,
        *,
        read_alias: str,
        write_alias: str,
        target_index: str,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Atomically point both workspace aliases to a validated v2 index."""
        if not self._client.indices.exists(index=target_index):
            raise RuntimeError(f"Elasticsearch target index not found: {target_index}")
        self.validate_index_contract(target_index)
        previous = self.get_alias_state([read_alias, write_alias])
        actions = _alias_removal_actions(previous)
        actions.extend(
            [
                {"add": {"index": target_index, "alias": read_alias}},
                {
                    "add": {
                        "index": target_index,
                        "alias": write_alias,
                        "is_write_index": True,
                    }
                },
            ]
        )
        self._client.indices.update_aliases(actions=actions)
        return previous

    def ensure_workspace_aliases(
        self,
        *,
        read_alias: str,
        write_alias: str,
        target_index: str,
    ) -> AliasEnsureResult:
        """Create missing workspace aliases without redirecting existing aliases."""
        if not self._client.indices.exists(index=target_index):
            raise RuntimeError(f"Elasticsearch target index not found: {target_index}")
        self.validate_index_contract(target_index)
        previous = self.get_alias_state([read_alias, write_alias])
        read_targets = previous[read_alias]
        write_targets = previous[write_alias]

        if read_targets and (
            set(read_targets) != {target_index}
            or read_targets[target_index].get("is_write_index") is True
        ):
            raise WorkspaceAliasConflictError(
                f"read alias {read_alias!r} has unexpected targets"
            )
        if write_targets and (
            set(write_targets) != {target_index}
            or write_targets[target_index].get("is_write_index") is not True
        ):
            raise WorkspaceAliasConflictError(
                f"write alias {write_alias!r} has unexpected targets"
            )

        actions: list[dict[str, dict[str, Any]]] = []
        if not read_targets:
            actions.append(
                {"add": {"index": target_index, "alias": read_alias}}
            )
        if not write_targets:
            actions.append(
                {
                    "add": {
                        "index": target_index,
                        "alias": write_alias,
                        "is_write_index": True,
                    }
                }
            )
        if actions:
            self._client.indices.update_aliases(actions=actions)
        self.validate_workspace_aliases(
            read_alias=read_alias,
            write_alias=write_alias,
            target_index=target_index,
        )
        return AliasEnsureResult(
            changed=bool(actions),
            previous_state=previous,
            final_state=self.get_alias_state([read_alias, write_alias]),
        )

    def validate_workspace_aliases(
        self,
        *,
        read_alias: str,
        write_alias: str,
        target_index: str,
    ) -> None:
        state = self.get_alias_state([read_alias, write_alias])
        read_targets = state[read_alias]
        write_targets = state[write_alias]
        if (
            set(read_targets) != {target_index}
            or read_targets[target_index].get("is_write_index") is True
        ):
            raise WorkspaceAliasConflictError(
                f"read alias {read_alias!r} is not ready"
            )
        if (
            set(write_targets) != {target_index}
            or write_targets[target_index].get("is_write_index") is not True
        ):
            raise WorkspaceAliasConflictError(
                f"write alias {write_alias!r} is not ready"
            )

    def restore_workspace_aliases(
        self, state: dict[str, dict[str, dict[str, Any]]]
    ) -> None:
        """Atomically restore a previously captured read/write alias state."""
        current = self.get_alias_state(list(state))
        actions = _alias_removal_actions(current)
        for alias, targets in state.items():
            write_targets = sum(
                config.get("is_write_index") is True for config in targets.values()
            )
            if write_targets > 1:
                raise ValueError(f"alias {alias!r} has multiple write targets")
            for index_name, config in targets.items():
                actions.append(
                    {
                        "add": {
                            "index": index_name,
                            "alias": alias,
                            **dict(config),
                        }
                    }
                )
        if actions:
            self._client.indices.update_aliases(actions=actions)

    def delete_index_if_exists(self, index_name: str) -> bool:
        if not self._client.indices.exists(index=index_name):
            return False
        self._client.indices.delete(index=index_name)
        return True

    def delete_indices_if_exist(
        self, index_names: list[str] | tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Delete all existing names in one Elasticsearch request."""
        existing = tuple(
            name
            for name in dict.fromkeys(index_names)
            if self._client.indices.exists(index=name)
        )
        missing = tuple(
            name
            for name in dict.fromkeys(index_names)
            if name not in existing
        )
        if existing:
            self._client.indices.delete(index=",".join(existing))
        return existing, missing

    def resolve_write_target(
        self,
        *,
        legacy_index: str,
        write_alias: str,
        chunk_index_mode: str,
    ) -> str:
        if chunk_index_mode == "legacy":
            return legacy_index
        if chunk_index_mode != "v2_alias":
            raise ValueError(f"Unsupported chunk index mode: {chunk_index_mode}")
        targets = self.get_alias_state([write_alias])[write_alias]
        write_indices = [
            index_name
            for index_name, config in targets.items()
            if config.get("is_write_index") is True
        ]
        if len(write_indices) != 1:
            raise RuntimeError(
                f"Elasticsearch write alias {write_alias!r} must have one write index"
            )
        self.validate_index_contract(write_indices[0])
        return write_alias

    def resolve_read_target(
        self,
        *,
        legacy_index: str,
        read_alias: str,
        chunk_index_mode: str,
    ) -> str:
        if chunk_index_mode == "legacy":
            return legacy_index
        if chunk_index_mode != "v2_alias":
            raise ValueError(f"Unsupported chunk index mode: {chunk_index_mode}")
        targets = self.get_alias_state([read_alias])[read_alias]
        if len(targets) != 1:
            raise RuntimeError(
                f"Elasticsearch read alias {read_alias!r} must point to one index"
            )
        physical_index = next(iter(targets))
        self.validate_index_contract(physical_index)
        return read_alias

    def bulk_upsert_chunks(self, index_name: str, docs: list[dict[str, Any]]) -> int:
        if not docs:
            return 0
        from elasticsearch.helpers import bulk

        for document in docs:
            validate_chunk_search_document(document)
        actions = []
        for d in docs:
            cid = d["chunk_id"]
            actions.append(
                {
                    "_op_type": "index",
                    "_index": index_name,
                    "_id": str(cid),
                    "_source": d,
                }
            )
        ok, errors = bulk(self._client, actions, refresh="wait_for", raise_on_error=False)
        if errors:
            logger.warning("Elasticsearch bulk had errors: %s", errors[:3])
        return int(ok)

    def bulk_delete_chunks(
        self, index_name: str, chunk_ids: list[str]
    ) -> tuple[int, list[Any]]:
        """Delete only the explicitly supplied Elasticsearch document IDs."""
        if not chunk_ids:
            return 0, []
        from elasticsearch.helpers import bulk

        actions = [
            {
                "_op_type": "delete",
                "_index": index_name,
                "_id": str(chunk_id),
            }
            for chunk_id in chunk_ids
        ]
        ok, errors = bulk(
            self._client,
            actions,
            refresh="wait_for",
            raise_on_error=False,
        )
        if errors:
            logger.warning("Elasticsearch bulk delete had errors: %s", errors[:3])
        return int(ok), list(errors or [])

    def delete_by_file_id(
        self, index_name: str, file_id: int, *, required: bool = False
    ) -> None:
        try:
            if not self._client.indices.exists(index=index_name):
                if required:
                    raise RuntimeError(
                        f"Elasticsearch index or alias not found: {index_name}"
                    )
                return
        except Exception:
            if required:
                raise
            return
        self._client.delete_by_query(
            index=index_name,
            query={"term": {"file_id": file_id}},
            refresh=True,
            conflicts="proceed",
        )

    def iter_chunk_sources(
        self, index_name: str, batch_size: int = 500
    ):
        response = self._client.search(
            index=index_name,
            size=batch_size,
            scroll="1m",
            query={"match_all": {}},
            source=True,
            sort=["_doc"],
        )
        scroll_id = response.get("_scroll_id")
        try:
            while True:
                hits = response.get("hits", {}).get("hits", [])
                if not hits:
                    break
                for hit in hits:
                    source = dict(hit.get("_source") or {})
                    source.setdefault("chunk_id", str(hit.get("_id") or ""))
                    yield source
                response = self._client.scroll(scroll_id=scroll_id, scroll="1m")
                scroll_id = response.get("_scroll_id", scroll_id)
        finally:
            if scroll_id:
                self._client.clear_scroll(scroll_id=scroll_id)

    def search_chunk_scores(
        self,
        *,
        index_names: list[str],
        query_text: str,
        file_ids: list[int],
        chunk_ids: list[str],
    ) -> dict[str, float]:
        """BM25 scores for chunk_ids restricted to file_ids; missing chunks omitted.

        This intentionally returns scores only, never chunk content.
        """
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
            track_total_hits=False,
        )
        out: dict[str, float] = {}
        for hit in resp.get("hits", {}).get("hits", []):
            cid = hit.get("_id")
            if cid is not None:
                out[str(cid)] = float(hit.get("_score") or 0.0)
        return out

    def search_chunks(
        self,
        *,
        index_names: list[str],
        query_text: str,
        file_ids: list[int],
        top_k: int,
        chunk_index_mode: str = "legacy",
    ) -> list[dict[str, Any]]:
        """Return independent BM25 hits within a finite authorized file scope."""
        if not index_names or not query_text.strip() or not file_ids or top_k <= 0:
            return []

        index_arg = ",".join(index_names)
        merged: dict[str, dict[str, Any]] = {}
        for start in range(0, len(file_ids), _FILE_SCOPE_BATCH_SIZE):
            file_batch = file_ids[start : start + _FILE_SCOPE_BATCH_SIZE]
            query = (
                _build_content_exact_query(query_text, file_batch)
                if chunk_index_mode == "v2_alias"
                else _build_legacy_content_query(query_text, file_batch)
            )
            response = self._client.search(
                index=index_arg,
                size=top_k,
                query=query,
                source=["file_id", "content"],
                track_total_hits=False,
            )
            for raw_hit in response.get("hits", {}).get("hits", []):
                chunk_id = raw_hit.get("_id")
                source = raw_hit.get("_source") or {}
                try:
                    file_id = int(source["file_id"])
                except (KeyError, TypeError, ValueError):
                    logger.warning(
                        "Elasticsearch sparse hit ignored: invalid file_id chunk_id=%s",
                        chunk_id,
                    )
                    continue
                if chunk_id is None:
                    continue
                raw_matched = raw_hit.get("matched_queries") or []
                if isinstance(raw_matched, dict):
                    matched_queries = sorted(str(name) for name in raw_matched)
                else:
                    matched_queries = [str(name) for name in raw_matched]
                hit = {
                    "chunk_id": str(chunk_id),
                    "file_id": file_id,
                    "text": str(source.get("content") or ""),
                    "sparse_score": float(raw_hit.get("_score") or 0.0),
                }
                if chunk_index_mode == "v2_alias":
                    hit["matched_queries"] = matched_queries
                    hit["exact_term_match"] = (
                        "exact_identifier" in matched_queries
                    )
                previous = merged.get(hit["chunk_id"])
                if previous is None or hit["sparse_score"] > previous["sparse_score"]:
                    merged[hit["chunk_id"]] = hit

        return sorted(
            merged.values(),
            key=lambda hit: (-hit["sparse_score"], hit["chunk_id"]),
        )[:top_k]


def _normalize_mapping_from_es(mapping: Any) -> Any:
    """Restore redundant values that Elasticsearch omits from get_mapping."""
    if not isinstance(mapping, dict):
        return mapping
    normalized = copy.deepcopy(mapping)
    for field in normalized.get("properties", {}).values():
        if (
            isinstance(field, dict)
            and field.get("type") == "text"
            and "analyzer" in field
            and "search_analyzer" not in field
        ):
            field["search_analyzer"] = field["analyzer"]
    return normalized


def _build_legacy_content_query(
    query_text: str, file_ids: list[int]
) -> dict[str, Any]:
    return {
        "bool": {
            "must": [
                {
                    "multi_match": {
                        "query": query_text,
                        "type": "best_fields",
                        "fields": ["content"],
                    }
                }
            ],
            "filter": [{"terms": {"file_id": file_ids}}],
        }
    }


def _build_content_exact_query(
    query_text: str, file_ids: list[int]
) -> dict[str, Any]:
    should: list[dict[str, Any]] = [
        {
            "match": {
                "content": {
                    "query": query_text,
                    "boost": 1.0,
                    "_name": "content_bm25",
                }
            }
        }
    ]
    exact_terms = extract_exact_terms(query_text)
    if exact_terms:
        should.append(
            {
                "terms": {
                    "exact_terms": exact_terms,
                    "boost": 8.0,
                    "_name": "exact_identifier",
                }
            }
        )
    return {
        "bool": {
            "should": should,
            "minimum_should_match": 1,
            "filter": [{"terms": {"file_id": file_ids}}],
        }
    }


def _alias_removal_actions(
    state: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, dict[str, Any]]]:
    return [
        {"remove": {"index": index_name, "alias": alias}}
        for alias, targets in state.items()
        for index_name in targets
    ]


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


def require_es_chunk_store_from_config() -> EsChunkStore:
    """Return a reachable store or fail when full-text indexing is required."""
    from openrag.config import get_config

    cfg = get_config().elasticsearch
    if not cfg.enabled:
        raise ElasticsearchRequiredError("Elasticsearch is disabled")
    hosts = [h.strip() for h in cfg.hosts.split(",") if h.strip()]
    if not hosts:
        raise ElasticsearchRequiredError("Elasticsearch hosts are empty")
    try:
        store = EsChunkStore(
            hosts,
            request_timeout=cfg.request_timeout,
            verify_certs=cfg.verify_certs,
        )
        if not store.ping():
            raise ElasticsearchRequiredError(
                f"Elasticsearch ping failed for hosts={hosts}"
            )
        return store
    except ElasticsearchRequiredError:
        raise
    except Exception as exc:
        raise ElasticsearchRequiredError(
            f"Elasticsearch client initialization failed: {exc}"
        ) from exc
