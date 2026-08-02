"""Retrieval service integrating Milvus vector search with permission filtering."""

import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.embedding.embedding_engine import EmbeddingEngine
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File
from openrag.models.workspace import Workspace

if TYPE_CHECKING:
    from openrag.search.es_chunk_store import EsChunkStore
    from openrag.vectorstore.milvus_layer_store import MilvusLayerStore

from openrag.retrieval.l1_llm_navigator import (
    L1LlmNavigationResult,
    _best_l1_text_per_file,
    filter_chunk_hits_by_indices,
    llm_select_chunk_indices,
)
from openrag.retrieval.hybrid_fusion import weighted_rrf
from openrag.retrieval.query_intent import infer_retrieval_strategy, normalize_strategy
from openrag.services.trace_service import TraceService

logger = logging.getLogger(__name__)

# B5: 轻量召回（L0 为主）时的默认窗口
_LIGHT_L0_TOP_N = 22
_LIGHT_L1_TOP_N = 15
_LIGHT_CHUNK_MULT = 2

_L1_LLM_META_APPLIED = "_l1_llm_applied"
_L1_LLM_META_SKIP_REASON = "_l1_llm_skip_reason"


class PermissionScopeResolutionError(RuntimeError):
    """Raised when the caller's readable file scope cannot be verified."""


@dataclass(frozen=True)
class ResolvedFileScope:
    """Explicit result of permission and requested-scope resolution."""

    allowed_file_ids: Optional[tuple[int, ...]]
    is_verified_global: bool
    resolution_status: str

    @classmethod
    def finite(cls, file_ids) -> "ResolvedFileScope":
        ids = tuple(dict.fromkeys(int(file_id) for file_id in file_ids))
        return cls(
            allowed_file_ids=ids,
            is_verified_global=False,
            resolution_status="empty" if not ids else "finite",
        )

    @classmethod
    def verified_global(cls) -> "ResolvedFileScope":
        return cls(
            allowed_file_ids=None,
            is_verified_global=True,
            resolution_status="verified_global",
        )

    @property
    def is_empty(self) -> bool:
        return self.allowed_file_ids == ()

    def backend_file_ids(self) -> Optional[list[int]]:
        if self.allowed_file_ids is None:
            return None
        return list(self.allowed_file_ids)


def l0_l1_retrieval_enabled() -> bool:
    """Return whether L0/L1 should participate in retrieval-time recall."""
    val = os.environ.get("OPENRAG_RETRIEVAL_USE_L0_L1", "true")
    return str(val).strip().lower() not in {"0", "false", "no", "off"}


def _annotate_l1_llm_meta(
    hits: list[dict], applied: bool, skip_reason: Optional[str]
) -> list[dict]:
    if hits:
        hits[0][_L1_LLM_META_APPLIED] = applied
        hits[0][_L1_LLM_META_SKIP_REASON] = skip_reason
    return hits


def _max_score_per_file(hits: list[dict]) -> dict[int, float]:
    out: dict[int, float] = {}
    for h in hits:
        fid = h.get("file_id")
        if fid is None:
            continue
        s = float(h.get("score", 0.0))
        cur = out.get(int(fid))
        if cur is None or s > cur:
            out[int(fid)] = s
    return out


def _minmax_norm(scores: dict[int, float]) -> dict[int, float]:
    if not scores:
        return {}
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return {k: 0.5 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def _preview_query(q: str, max_len: int = 120) -> str:
    s = " ".join((q or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _milvus_score_stats(hits: list[dict]) -> tuple[float, float, float]:
    sc = [float(h.get("score", 0)) for h in hits]
    if not sc:
        return 0.0, 0.0, 0.0
    return min(sc), max(sc), sum(sc) / len(sc)


def _top_hits_line(hits: list[dict], *, n: int = 3, score_key: str) -> str:
    if not hits:
        return "-"
    ranked = sorted(hits, key=lambda h: float(h.get(score_key, 0)), reverse=True)[:n]
    parts: list[str] = []
    for h in ranked:
        parts.append(
            "chunk_id=%s file_id=%s %s=%.4f"
            % (
                h.get("chunk_id"),
                h.get("file_id"),
                score_key,
                float(h.get(score_key, 0)),
            )
        )
    return "; ".join(parts)


class RetrievalService:
    """Provides semantic search over ingested documents with permission filtering."""

    def __init__(
        self,
        db: Session,
        embedding_engine: EmbeddingEngine,
        vector_store,
        layer_store: Optional["MilvusLayerStore"] = None,
        fulltext_store: Optional["EsChunkStore"] = None,
        hybrid_recall_mode: Optional[str] = None,
        chunk_index_mode: Optional[str] = None,
    ):
        self.db = db
        self.embedding_engine = embedding_engine
        self.vector_store = vector_store
        self.layer_store = layer_store
        self.fulltext_store = fulltext_store
        if hybrid_recall_mode is None or chunk_index_mode is None:
            from openrag.config import get_config

            es_config = get_config().elasticsearch
            if hybrid_recall_mode is None:
                hybrid_recall_mode = es_config.hybrid_recall_mode
            if chunk_index_mode is None:
                chunk_index_mode = getattr(es_config, "chunk_index_mode", "legacy")
        self.hybrid_recall_mode = hybrid_recall_mode
        self.chunk_index_mode = chunk_index_mode or "legacy"
        self.trace_service = TraceService(db)

    def search(
        self,
        query: str,
        user_id: int,
        workspace_id: Optional[int] = None,
        top_k: int = 10,
        use_contextual: bool = False,
        contextual_l0_top_n: int = 40,
        contextual_l1_top_n: int = 30,
        contextual_chunk_fetch_multiplier: int = 4,
        retrieval_strategy: str = "auto",
        use_l1_llm_navigation: bool = False,
        vector_similarity_weight: float = 1.0,
        scope_file_ids: Optional[set[int]] = None,
    ) -> list[dict]:
        """Search for relevant chunks.

        use_contextual: L0 粗筛 → L1 辅助 → 仅在候选 file_id 上搜 L2，并按
        0.2*L0 + 0.3*L1 + 0.5*L2 融合排序（分数各自 min-max 归一化）。

        retrieval_strategy: auto|light|deep|precise|flat（B5 意图路由）。
        precise/flat 在开启上下文时退化为加宽 top_k 的平面切片检索。
        use_l1_llm_navigation: B7/B8，仅在上下文检索且可用 OPENAI_API_KEY 时，用 LLM 根据 L1 选择 chunk 下标并过滤向量命中。
        """
        if not l0_l1_retrieval_enabled():
            use_contextual = False
            use_l1_llm_navigation = False

        resolved_scope = self._effective_file_scope(
            user_id, workspace_id, scope_file_ids
        )
        if resolved_scope.is_empty:
            return []

        setting = normalize_strategy(retrieval_strategy)
        strat = infer_retrieval_strategy(query) if setting == "auto" else setting
        flat_top_k = min(top_k * 2, 100) if strat == "precise" else top_k

        def _tag(hits: list[dict]) -> list[dict]:
            for h in hits:
                h["retrieval_strategy"] = strat
            return hits

        l1_llm_applied = False
        l1_llm_skip_reason: Optional[str] = None

        if not use_contextual or self.layer_store is None:
            if use_contextual and self.layer_store is None:
                logger.warning(
                    "use_contextual=True but layer_store unavailable; flat chunk search"
                )
            if use_l1_llm_navigation:
                l1_llm_skip_reason = "not_contextual"
            hits = _tag(
                self._search_flat(
                    query,
                    user_id,
                    workspace_id,
                    flat_top_k,
                    vector_similarity_weight=vector_similarity_weight,
                    scope_file_ids=scope_file_ids,
                    resolved_scope=resolved_scope,
                )
            )
            hits = _annotate_l1_llm_meta(hits, l1_llm_applied, l1_llm_skip_reason)
            return hits

        if strat in ("precise", "flat"):
            if use_l1_llm_navigation:
                l1_llm_skip_reason = "strategy_not_contextual"
            hits = _tag(
                self._search_flat(
                    query,
                    user_id,
                    workspace_id,
                    flat_top_k,
                    vector_similarity_weight=vector_similarity_weight,
                    scope_file_ids=scope_file_ids,
                    resolved_scope=resolved_scope,
                )
            )
            hits = _annotate_l1_llm_meta(hits, l1_llm_applied, l1_llm_skip_reason)
            return hits

        if strat == "light":
            l0n, l1n, m = _LIGHT_L0_TOP_N, _LIGHT_L1_TOP_N, _LIGHT_CHUNK_MULT
        else:
            l0n = contextual_l0_top_n
            l1n = contextual_l1_top_n
            m = contextual_chunk_fetch_multiplier

        hits = _tag(
            self._search_contextual(
                query=query,
                user_id=user_id,
                workspace_id=workspace_id,
                top_k=top_k,
                l0_top_n=l0n,
                l1_top_n=l1n,
                chunk_fetch_multiplier=m,
                use_l1_llm_navigation=use_l1_llm_navigation,
                vector_similarity_weight=vector_similarity_weight,
                scope_file_ids=scope_file_ids,
                resolved_scope=resolved_scope,
            )
        )
        # _search_contextual sets _l1_llm_applied / _l1_llm_skip_reason on first hit
        return hits

    def _search_flat(
        self,
        query: str,
        user_id: int,
        workspace_id: Optional[int],
        top_k: int,
        *,
        vector_similarity_weight: float = 1.0,
        scope_file_ids: Optional[set[int]] = None,
        resolved_scope: Optional[ResolvedFileScope] = None,
    ) -> list[dict]:
        scope = resolved_scope or self._effective_file_scope(
            user_id, workspace_id, scope_file_ids
        )
        if scope.is_empty:
            return []
        accessible_file_ids = scope.backend_file_ids()
        query_vec = self._embed_query(query)

        dense_stage = (
            "retrieval.dense_recall"
            if self.hybrid_recall_mode == "independent_rrf"
            else "retrieval.chunk_search"
        )
        self.trace_service.start_span(
            dense_stage,
            input_summary={
                "top_k": top_k,
                "file_filter_count": (
                    len(accessible_file_ids) if accessible_file_ids is not None else None
                ),
            },
        )
        try:
            hits = self.vector_store.search(
                query_embedding=query_vec,
                top_k=top_k,
                file_ids=accessible_file_ids,
            )
            self._record_chunk_search_snapshots(hits, stage=dense_stage)
            self.trace_service.finish_span(output_summary={"hit_count": len(hits)})
        except Exception as exc:
            self.trace_service.fail_span(error_message=str(exc))
            raise
        for h in hits:
            h["retrieval_mode"] = "flat"
            h["dense_score"] = h.get("score")

        mn, mx, av = _milvus_score_stats(hits)
        filt = len(accessible_file_ids) if accessible_file_ids is not None else "all"
        logger.info(
            "retrieval_vector flat query_preview=%r workspace_id=%s top_k=%d "
            "file_id_filter=%s milvus_hits=%d milvus_score_min=%.4f max=%.4f mean=%.4f "
            "milvus_top3=[%s]",
            _preview_query(query),
            workspace_id,
            top_k,
            filt,
            len(hits),
            mn,
            mx,
            av,
            _top_hits_line(hits, n=3, score_key="score"),
        )

        wvec = float(vector_similarity_weight)
        if wvec >= 1.0 - 1e-12:
            hits, _stats = self._validate_and_enrich_hits(
                hits, workspace_id=workspace_id, resolved_scope=scope
            )
            return hits

        if self.hybrid_recall_mode == "independent_rrf":
            sparse_hits, sparse_state = self._recall_sparse(
                query=query,
                workspace_id=workspace_id,
                resolved_scope=scope,
                top_k=top_k,
            )
            if sparse_state["degraded"] or sparse_state["skip_reason"] is not None:
                combined = hits
            else:
                combined = self._merge_independent_recalls(
                    hits,
                    sparse_hits,
                    dense_weight=wvec,
                    retrieval_mode="flat",
                )
            combined, _stats = self._validate_and_enrich_hits(
                combined, workspace_id=workspace_id, resolved_scope=scope
            )
            return combined[:top_k]

        scores = [float(h.get("score", 0)) for h in hits]
        if scores:
            smin, smax = min(scores), max(scores)
            if smax > smin:
                for h in hits:
                    s = float(h.get("score", 0))
                    nv = (s - smin) / (smax - smin)
                    h["fused_score"] = nv
            else:
                for h in hits:
                    h["fused_score"] = 0.5
        else:
            for h in hits:
                h["fused_score"] = 0.0

        if accessible_file_ids is not None:
            es_file_ids = list(accessible_file_ids)
        else:
            es_file_ids = list(
                {int(h["file_id"]) for h in hits if h.get("file_id") is not None}
            )
        self._blend_elasticsearch_scores(
            query,
            hits,
            es_file_ids,
            workspace_id,
            vector_similarity_weight,
        )
        hits.sort(
            key=lambda x: float(x.get("fused_score", x.get("score", 0.0))),
            reverse=True,
        )
        hits, _stats = self._validate_and_enrich_hits(
            hits, workspace_id=workspace_id, resolved_scope=scope
        )
        logger.info(
            "retrieval_flat_after_fusion top3=[%s]",
            _top_hits_line(hits, n=3, score_key="reranked_score"),
        )
        return hits

    def _recall_sparse(
        self,
        *,
        query: str,
        workspace_id: Optional[int],
        resolved_scope: ResolvedFileScope,
        top_k: int,
    ) -> tuple[list[dict], dict[str, object]]:
        from openrag.search.es_chunk_contract import (
            QUERY_PROFILE_VERSION,
            SCHEMA_VERSION,
            compute_mapping_hash,
            extract_exact_terms,
        )

        is_v2 = self.chunk_index_mode == "v2_alias"
        state: dict[str, object] = {
            "degraded": False,
            "skip_reason": None,
            "chunk_index_mode": self.chunk_index_mode,
            "physical_index_names": [],
            "schema_version": SCHEMA_VERSION if is_v2 else None,
            "mapping_hash": compute_mapping_hash() if is_v2 else None,
            "query_profile_version": (
                QUERY_PROFILE_VERSION if is_v2 else "legacy-content"
            ),
            "query_exact_term_count": (
                len(extract_exact_terms(query)) if is_v2 else 0
            ),
        }
        self.trace_service.start_span(
            "retrieval.sparse_recall",
            input_summary={
                "top_k": top_k,
                "file_filter_count": (
                    len(resolved_scope.allowed_file_ids or ())
                    if not resolved_scope.is_verified_global
                    else None
                ),
            },
        )
        if self.fulltext_store is None:
            state.update(degraded=True, skip_reason="no_fulltext_store")
            self.trace_service.finish_span(output_summary={"hit_count": 0, **state})
            return [], state
        if resolved_scope.is_verified_global:
            state.update(degraded=False, skip_reason="verified_global_scope")
            self.trace_service.finish_span(output_summary={"hit_count": 0, **state})
            return [], state
        file_ids = list(resolved_scope.allowed_file_ids or ())
        if not file_ids:
            state.update(degraded=False, skip_reason="empty_scope")
            self.trace_service.finish_span(output_summary={"hit_count": 0, **state})
            return [], state
        index_names = self._resolve_es_index_names(workspace_id, file_ids)
        if not index_names:
            state.update(degraded=True, skip_reason="no_index_names")
            self.trace_service.finish_span(output_summary={"hit_count": 0, **state})
            return [], state
        if is_v2:
            alias_state = self.fulltext_store.get_alias_state(index_names)
            state["physical_index_names"] = sorted(
                {
                    physical
                    for targets in alias_state.values()
                    for physical in targets
                }
            )
        else:
            state["physical_index_names"] = list(index_names)
        started = time.perf_counter()
        try:
            search_kwargs = dict(
                index_names=index_names,
                query_text=query,
                file_ids=file_ids,
                top_k=top_k,
            )
            if is_v2:
                search_kwargs["chunk_index_mode"] = self.chunk_index_mode
            hits = self.fulltext_store.search_chunks(**search_kwargs)
        except Exception as exc:
            state.update(degraded=True, skip_reason="sparse_error")
            self.trace_service.finish_span(
                output_summary={"hit_count": 0, "error": str(exc), **state},
                metrics={"latency_ms": (time.perf_counter() - started) * 1000.0},
            )
            logger.warning("Independent sparse recall failed: %s", exc)
            return [], state
        self.trace_service.finish_span(
            output_summary={"hit_count": len(hits), **state},
            metrics={"latency_ms": (time.perf_counter() - started) * 1000.0},
        )
        for rank, hit in enumerate(hits[:50], start=1):
            self.trace_service.record_snapshot(
                stage="retrieval.sparse_recall",
                rank=rank,
                chunk_id=_safe_chunk_id(hit),
                file_id=_safe_int(hit.get("file_id")),
                score=_safe_float(hit.get("sparse_score")),
                score_parts={"sparse_score": _safe_float(hit.get("sparse_score"))},
                metadata={
                    "phase": "output",
                    "matched_queries": list(hit.get("matched_queries") or []),
                    "exact_term_match": bool(hit.get("exact_term_match")),
                    "schema_version": state["schema_version"],
                    "mapping_hash": state["mapping_hash"],
                    "query_profile_version": state["query_profile_version"],
                },
            )
        return hits, state

    def _merge_independent_recalls(
        self,
        dense_hits: list[dict],
        sparse_hits: list[dict],
        *,
        dense_weight: float,
        retrieval_mode: str,
    ) -> list[dict]:
        dense_ids = {
            str(hit.get("chunk_id")) for hit in dense_hits if hit.get("chunk_id")
        }
        sparse_ids = {
            str(hit.get("chunk_id")) for hit in sparse_hits if hit.get("chunk_id")
        }
        self.trace_service.start_span(
            "retrieval.hybrid_fusion",
            input_summary={
                "dense_count": len(dense_ids),
                "sparse_count": len(sparse_ids),
                "dense_weight": dense_weight,
                "rrf_k": 60,
            },
        )
        fused = weighted_rrf(
            dense_hits, sparse_hits, dense_weight=dense_weight, rrf_k=60
        )
        for hit in fused:
            hit["retrieval_mode"] = retrieval_mode
        for rank, hit in enumerate(fused[:50], start=1):
            self.trace_service.record_snapshot(
                stage="retrieval.hybrid_fusion",
                rank=rank,
                chunk_id=_safe_chunk_id(hit),
                file_id=_safe_int(hit.get("file_id")),
                score=_safe_float(hit.get("fused_score")),
                score_parts={
                    "dense_score": _safe_float(hit.get("dense_score")),
                    "sparse_score": _safe_float(hit.get("sparse_score")),
                    "dense_rank": hit.get("dense_rank"),
                    "sparse_rank": hit.get("sparse_rank"),
                    "fused_score": _safe_float(hit.get("fused_score")),
                },
                metadata={
                    "phase": "output",
                    "recall_sources": hit.get("recall_sources") or [],
                },
            )
        self.trace_service.finish_span(
            output_summary={
                "dense_count": len(dense_ids),
                "sparse_count": len(sparse_ids),
                "overlap_count": len(dense_ids & sparse_ids),
                "dense_only_count": len(dense_ids - sparse_ids),
                "sparse_only_count": len(sparse_ids - dense_ids),
                "union_count": len(fused),
            }
        )
        return fused

    def _search_contextual(
        self,
        query: str,
        user_id: int,
        workspace_id: Optional[int],
        top_k: int,
        l0_top_n: int,
        l1_top_n: int,
        chunk_fetch_multiplier: int,
        use_l1_llm_navigation: bool = False,
        vector_similarity_weight: float = 1.0,
        scope_file_ids: Optional[set[int]] = None,
        resolved_scope: Optional[ResolvedFileScope] = None,
    ) -> list[dict]:
        scope = resolved_scope or self._effective_file_scope(
            user_id, workspace_id, scope_file_ids
        )
        if scope.is_empty:
            return []
        accessible = scope.backend_file_ids()
        query_vec = self._embed_query(query)

        # Always hand the effective file ids to the layer store so a large scope
        # (>512) uses its oversample+post-filter path instead of an unfiltered
        # full-workspace L0 top-N that could squeeze out in-scope candidates.
        use_expr_filter = accessible is not None and len(accessible) <= 512
        l0_file_filter = accessible if accessible is not None else None
        l0_cap = max(l0_top_n * 4, 80)

        l0_hits = self.layer_store.search_layers(
            query_vec, "l0", top_k=l0_cap, file_ids=l0_file_filter
        )
        if accessible is not None and not use_expr_filter:
            acc_set = set(accessible)
            l0_hits = [h for h in l0_hits if h.get("file_id") in acc_set]
        l0_hits.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
        l0_hits = l0_hits[:l0_top_n]

        file_l0_raw = _max_score_per_file(l0_hits)
        candidate_files = list(file_l0_raw.keys())
        candidate_files = self._filter_to_active_file_ids(candidate_files)  # round-4 #2
        if not candidate_files:
            logger.info("Contextual search: no active L0 hits; fallback to flat chunk search")
            return self._search_flat(
                query,
                user_id,
                workspace_id,
                top_k,
                vector_similarity_weight=vector_similarity_weight,
                scope_file_ids=scope_file_ids,
                resolved_scope=scope,
            )

        l1_hits = self.layer_store.search_layers(
            query_vec,
            "l1",
            top_k=max(l1_top_n, len(candidate_files) * 3),
            file_ids=candidate_files,
        )
        file_l1_raw = _max_score_per_file(l1_hits)

        l1_llm_result = L1LlmNavigationResult()
        if use_l1_llm_navigation:
            l1_by_file = _best_l1_text_per_file(l1_hits)
            summaries: list[dict] = []
            for fid in candidate_files:
                frow = self.db.query(File).filter(File.id == fid).first()
                if not frow:
                    continue
                summaries.append(
                    {
                        "file_id": int(fid),
                        "name": frow.name,
                        "chunk_count": int(frow.total_chunks or 0),
                        "l1_text": l1_by_file.get(int(fid), ""),
                    }
                )
            l1_llm_result = llm_select_chunk_indices(query, summaries)

        restrictions = l1_llm_result.restrictions
        l1_llm_applied = l1_llm_result.applied
        l1_llm_skip_reason = l1_llm_result.skip_reason

        fetch_k = min(200, max(top_k * chunk_fetch_multiplier, top_k))
        dense_stage = (
            "retrieval.dense_recall"
            if self.hybrid_recall_mode == "independent_rrf"
            else "retrieval.chunk_search"
        )
        self.trace_service.start_span(
            dense_stage,
            input_summary={"top_k": fetch_k, "file_filter_count": len(candidate_files)},
        )
        try:
            chunk_hits = self.vector_store.search(
                query_embedding=query_vec,
                top_k=fetch_k,
                file_ids=candidate_files,
            )
            self._record_chunk_search_snapshots(chunk_hits, stage=dense_stage)
            self.trace_service.finish_span(output_summary={"hit_count": len(chunk_hits)})
        except Exception as exc:
            self.trace_service.fail_span(error_message=str(exc))
            raise
        self._enrich_hits(chunk_hits)

        if restrictions:
            chunk_hits = filter_chunk_hits_by_indices(chunk_hits, restrictions)
            for h in chunk_hits:
                h["l1_llm_filtered"] = True

        norm_l0 = _minmax_norm(
            {fid: file_l0_raw[fid] for fid in candidate_files if fid in file_l0_raw}
        )
        norm_l1 = _minmax_norm(file_l1_raw) if file_l1_raw else {}
        chunk_scores = [float(h.get("score", 0)) for h in chunk_hits]
        if chunk_scores:
            cmin, cmax = min(chunk_scores), max(chunk_scores)
            if cmax > cmin:
                norm_chunk = [(s - cmin) / (cmax - cmin) for s in chunk_scores]
            else:
                norm_chunk = [0.5] * len(chunk_scores)
        else:
            norm_chunk = []

        w0, w1, w2 = 0.2, 0.3, 0.5
        for i, hit in enumerate(chunk_hits):
            fid = int(hit.get("file_id", 0))
            s0 = norm_l0.get(fid, 0.0)
            s1 = norm_l1.get(fid, 0.0)
            s2 = norm_chunk[i] if i < len(norm_chunk) else 0.0
            fused = w0 * s0 + w1 * s1 + w2 * s2
            hit["dense_score"] = hit.get("score")
            hit["context_score"] = fused
            hit["fused_score"] = fused
            hit["retrieval_mode"] = "contextual"

        l2_mn, l2_mx, l2_av = _milvus_score_stats(chunk_hits)
        logger.info(
            "retrieval_vector contextual query_preview=%r workspace_id=%s "
            "l0_kept=%d l1_files=%d candidate_files=%d fetch_k=%d l2_hits=%d "
            "milvus_L2_min=%.4f max=%.4f mean=%.4f vector_fusion_top3=[%s]",
            _preview_query(query),
            workspace_id,
            len(l0_hits),
            len(file_l1_raw),
            len(candidate_files),
            fetch_k,
            len(chunk_hits),
            l2_mn,
            l2_mx,
            l2_av,
            _top_hits_line(chunk_hits, n=3, score_key="fused_score"),
        )

        chunk_hits.sort(key=lambda x: float(x.get("context_score", 0)), reverse=True)
        if self.hybrid_recall_mode == "independent_rrf":
            wvec = float(vector_similarity_weight)
            if wvec >= 1.0 - 1e-12:
                chunk_hits, _stats = self._validate_and_enrich_hits(
                    chunk_hits, workspace_id=workspace_id, resolved_scope=scope
                )
                result = chunk_hits[:top_k]
                return _annotate_l1_llm_meta(
                    result, l1_llm_applied, l1_llm_skip_reason
                )
            sparse_hits, sparse_state = self._recall_sparse(
                query=query,
                workspace_id=workspace_id,
                resolved_scope=scope,
                top_k=fetch_k,
            )
            if sparse_state["degraded"] or sparse_state["skip_reason"] is not None:
                combined = chunk_hits
            else:
                combined = self._merge_independent_recalls(
                    chunk_hits,
                    sparse_hits,
                    dense_weight=wvec,
                    retrieval_mode="contextual",
                )
            combined, _stats = self._validate_and_enrich_hits(
                combined, workspace_id=workspace_id, resolved_scope=scope
            )
            result = combined[:top_k]
            return _annotate_l1_llm_meta(
                result, l1_llm_applied, l1_llm_skip_reason
            )

        self._blend_elasticsearch_scores(
            query,
            chunk_hits,
            candidate_files,
            workspace_id,
            vector_similarity_weight,
        )
        chunk_hits.sort(key=lambda x: float(x.get("fused_score", 0)), reverse=True)
        chunk_hits, _stats = self._validate_and_enrich_hits(
            chunk_hits, workspace_id=workspace_id, resolved_scope=scope
        )
        logger.info(
            "retrieval_contextual_after_fusion top_k_return=%d top3=[%s]",
            top_k,
            _top_hits_line(chunk_hits[:top_k], n=3, score_key="fused_score"),
        )
        result = chunk_hits[:top_k]
        return _annotate_l1_llm_meta(result, l1_llm_applied, l1_llm_skip_reason)

    def _resolve_es_index_names(
        self, workspace_id: Optional[int], file_ids: list[int]
    ) -> list[str]:
        from openrag.search.workspace_es_slug import (
            build_workspace_chunks_index_name,
            build_workspace_chunks_read_alias,
        )

        def target_for(slug: str, workspace_id_value: int) -> str:
            legacy_index = build_workspace_chunks_index_name(
                slug, workspace_id_value
            )
            read_alias = build_workspace_chunks_read_alias(
                slug, workspace_id_value
            )
            if self.chunk_index_mode == "legacy":
                return legacy_index
            if self.fulltext_store is None:
                raise RuntimeError("Elasticsearch store unavailable")
            return self.fulltext_store.resolve_read_target(
                legacy_index=legacy_index,
                read_alias=read_alias,
                chunk_index_mode=self.chunk_index_mode,
            )

        if workspace_id is not None:
            ws = self.db.query(Workspace).filter(Workspace.id == workspace_id).first()
            if ws:
                return [target_for(ws.slug, ws.id)]
            return []
        if not file_ids:
            return []
        rows = self.db.execute(
            select(Workspace.slug, Workspace.id)
            .join(File, File.workspace_id == Workspace.id)
            .where(File.id.in_(file_ids))
            .distinct()
        ).all()
        seen: set[str] = set()
        out: list[str] = []
        for slug, wid in rows:
            name = target_for(str(slug), int(wid))
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def _blend_elasticsearch_scores(
        self,
        query: str,
        hits: list[dict],
        filter_file_ids: list[int],
        workspace_id: Optional[int],
        vector_similarity_weight: float,
    ) -> None:
        """Combine S_v (fused_score or flat norm) with ES BM25 min-max; w=1 skips ES."""
        w = float(vector_similarity_weight)
        if w >= 1.0 - 1e-12:
            return
        self.trace_service.start_span(
            "retrieval.es_fusion",
            input_summary={
                "vector_similarity_weight": w,
                "bm25_weight": 1.0 - w,
                "candidate_count": len(hits),
            },
        )
        if self.fulltext_store is None:
            logger.info(
                "retrieval_es skip reason=no_fulltext_store w_vector=%.4f w_text=%.4f "
                "query_preview=%r hits=%d",
                w,
                1.0 - w,
                _preview_query(query),
                len(hits),
            )
            self.trace_service.finish_span(
                output_summary={"skip_reason": "no_fulltext_store"}
            )
            return
        if not hits or not filter_file_ids:
            logger.info(
                "retrieval_es skip reason=no_hits_or_no_file_filter w_vector=%.4f "
                "hits=%d filter_file_ids=%d",
                w,
                len(hits),
                len(filter_file_ids),
            )
            self.trace_service.finish_span(
                output_summary={"skip_reason": "no_hits_or_no_file_filter"}
            )
            return
        chunk_ids = [str(h.get("chunk_id") or "") for h in hits if h.get("chunk_id")]
        if not chunk_ids:
            logger.info(
                "retrieval_es skip reason=no_chunk_ids w_vector=%.4f hits=%d",
                w,
                len(hits),
            )
            self.trace_service.finish_span(output_summary={"skip_reason": "no_chunk_ids"})
            return
        index_names = self._resolve_es_index_names(workspace_id, filter_file_ids)
        if not index_names:
            logger.info(
                "retrieval_es skip reason=no_index_names workspace_id=%s "
                "w_vector=%.4f filter_file_ids=%d",
                workspace_id,
                w,
                len(filter_file_ids),
            )
            self.trace_service.finish_span(output_summary={"skip_reason": "no_index_names"})
            return
        t0 = time.perf_counter()
        try:
            es_scores = self.fulltext_store.search_chunk_scores(
                index_names=index_names,
                query_text=query,
                file_ids=filter_file_ids,
                chunk_ids=chunk_ids,
            )
        except Exception as exc:
            logger.warning(
                "retrieval_es error query_preview=%r indices=%s err=%s",
                _preview_query(query),
                index_names,
                exc,
            )
            self.trace_service.finish_span(
                output_summary={"skip_reason": "error", "error": str(exc)}
            )
            return
        es_ms = (time.perf_counter() - t0) * 1000.0
        raw = [float(es_scores.get(str(h.get("chunk_id") or ""), 0.0)) for h in hits]
        nonzero = sum(1 for x in raw if x > 0.0)
        tmin, tmax = min(raw), max(raw)
        if tmax > tmin:
            norm_t = [(x - tmin) / (tmax - tmin) for x in raw]
        else:
            norm_t = [0.5] * len(raw)

        self._record_fusion_snapshots(hits, norm_t, phase="input")
        fusion_rows: list[tuple[float, float, float, str, object]] = []
        for i, hit in enumerate(hits):
            s_v = float(hit.get("fused_score", hit.get("score", 0.0)))
            s_t = norm_t[i] if i < len(norm_t) else 0.5
            fused = w * s_v + (1.0 - w) * s_t
            hit["bm25_score"] = s_t
            hit["fused_score"] = fused
            cid = str(hit.get("chunk_id") or "")
            fusion_rows.append((fused, s_v, s_t, cid, hit.get("file_id")))

        fusion_rows.sort(key=lambda r: r[0], reverse=True)
        ranked_hits = sorted(
            hits,
            key=lambda h: float(h.get("fused_score", h.get("reranked_score", 0.0))),
            reverse=True,
        )
        self._record_fusion_snapshots(ranked_hits, None, phase="output")
        top5 = fusion_rows[:5]
        top5_s = "; ".join(
            "cid=%s fid=%s S=%.4f(Sv=%.4f,St=%.4f)" % (cid, fid, fs, sv, st)
            for fs, sv, st, cid, fid in top5
        )
        logger.info(
            "retrieval_es query_preview=%r indices=%s took_ms=%.1f "
            "chunk_candidates=%d es_returned_scores=%d es_nonzero_raw=%d "
            "es_raw_min=%.4f es_raw_max=%.4f file_id_filter_count=%d",
            _preview_query(query),
            index_names,
            es_ms,
            len(chunk_ids),
            len(es_scores),
            nonzero,
            tmin,
            tmax,
            len(filter_file_ids),
        )
        logger.info(
            "retrieval_fusion w_vector=%.4f w_text=%.4f formula=S=w*Sv+(1-w)*St "
            "top5_by_final=[%s]",
            w,
            1.0 - w,
            top5_s or "-",
        )
        self.trace_service.finish_span(
            output_summary={
                "candidate_count": len(hits),
                "es_returned_scores": len(es_scores),
                "skip_reason": None,
            },
            metrics={"took_ms": es_ms},
        )

    def _enrich_hits(self, hits: list[dict]) -> None:
        """Batch-enrich trusted hits; final filtering belongs to DB validation."""
        chunk_ids = [h.get("chunk_id") for h in hits if h.get("chunk_id")]
        chunk_rows: dict[str, DocumentChunk] = {}
        if chunk_ids:
            rows = (
                self.db.query(DocumentChunk)
                .filter(DocumentChunk.chunk_id.in_(chunk_ids))
                .all()
            )
            chunk_rows = {r.chunk_id: r for r in rows}

        file_ids = {row.file_id for row in chunk_rows.values()}
        file_rows = {
            row.id: row
            for row in self.db.query(File).filter(File.id.in_(file_ids)).all()
        } if file_ids else {}
        for hit in hits:
            row = chunk_rows.get(str(hit.get("chunk_id") or ""))
            if row:
                self._apply_enrichment(hit, row, file_rows.get(row.file_id))

    def _validate_and_enrich_hits(
        self,
        hits: list[dict],
        *,
        workspace_id: Optional[int],
        resolved_scope: ResolvedFileScope,
    ) -> tuple[list[dict], dict[str, int]]:
        """Validate candidates against authoritative chunk, file, and scope state."""
        stats = {
            "stale_hit_count": 0,
            "unauthorized_hit_count": 0,
            "ownership_mismatch_count": 0,
            "stale_sparse_hit_count": 0,
            "unauthorized_sparse_hit_count": 0,
        }
        self.trace_service.start_span(
            "retrieval.db_validation", input_summary={"candidate_count": len(hits)}
        )
        chunk_ids = list(
            dict.fromkeys(
                str(hit.get("chunk_id"))
                for hit in hits
                if hit.get("chunk_id") is not None
            )
        )
        chunks = {
            row.chunk_id: row
            for row in self.db.query(DocumentChunk)
            .filter(DocumentChunk.chunk_id.in_(chunk_ids))
            .all()
        } if chunk_ids else {}
        file_ids = {row.file_id for row in chunks.values()}
        files = {
            row.id: row
            for row in self.db.query(File).filter(File.id.in_(file_ids)).all()
        } if file_ids else {}
        allowed = (
            None
            if resolved_scope.is_verified_global
            else set(resolved_scope.allowed_file_ids or ())
        )
        validated: list[dict] = []
        seen: set[str] = set()

        for hit in hits:
            chunk_id = str(hit.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            is_sparse = "sparse" in (hit.get("recall_sources") or [])
            chunk = chunks.get(chunk_id)
            file_row = files.get(chunk.file_id) if chunk is not None else None
            if chunk is None or file_row is None or file_row.deleted_at is not None:
                stats["stale_hit_count"] += 1
                if is_sparse:
                    stats["stale_sparse_hit_count"] += 1
                continue
            try:
                claimed_file_id = int(hit.get("file_id"))
            except (TypeError, ValueError):
                claimed_file_id = None
            if (
                claimed_file_id != chunk.file_id
                or chunk.workspace_id != file_row.workspace_id
                or (workspace_id is not None and chunk.workspace_id != workspace_id)
                or (workspace_id is not None and file_row.workspace_id != workspace_id)
            ):
                stats["ownership_mismatch_count"] += 1
                continue
            if allowed is not None and chunk.file_id not in allowed:
                stats["unauthorized_hit_count"] += 1
                if is_sparse:
                    stats["unauthorized_sparse_hit_count"] += 1
                continue
            self._apply_enrichment(hit, chunk, file_row)
            validated.append(hit)

        stats["valid_hit_count"] = len(validated)
        self.trace_service.finish_span(output_summary=stats)
        return validated, stats

    @staticmethod
    def _apply_enrichment(
        hit: dict, row: DocumentChunk, file_row: Optional[File]
    ) -> None:
        if file_row is not None:
            hit["filename"] = file_row.name
            hit["uri"] = file_row.uri
        hit["object_key"] = row.object_key
        hit["object_url"] = row.object_url
        hit["local_chunk_path"] = row.local_chunk_path
        hit["chunk_index"] = row.chunk_index
        if row.text_preview:
            hit["text_preview"] = row.text_preview
        hit["page"] = row.page
        hit["level"] = row.level
        hit["block_type"] = row.block_type
        hit["start_offset"] = row.start_offset
        hit["end_offset"] = row.end_offset
        if row.bbox_x0 is not None:
            hit["bbox_x0"] = row.bbox_x0
            hit["bbox_y0"] = row.bbox_y0
            hit["bbox_x1"] = row.bbox_x1
            hit["bbox_y1"] = row.bbox_y1
        if row.source_block_id:
            hit["source_block_id"] = row.source_block_id
        if row.source_char_start is not None:
            hit["source_char_start"] = row.source_char_start
        if row.source_char_end is not None:
            hit["source_char_end"] = row.source_char_end

    def _accessible_file_ids(
        self, user_id: int, workspace_id: Optional[int] = None
    ) -> ResolvedFileScope:
        """Resolve a verified readable file scope or fail closed."""
        from openrag.models.user import User

        try:
            user = self.db.query(User).filter(User.id == user_id).first()
            if user and getattr(user, "is_admin", False):
                if workspace_id is not None:
                    rows = (
                        self.db.execute(
                            select(File.id).where(
                                File.workspace_id == workspace_id,
                                File.deleted_at.is_(None),
                            )
                        )
                        .scalars()
                        .all()
                    )
                    return ResolvedFileScope.finite(rows)
                return ResolvedFileScope.verified_global()

            if workspace_id is not None:
                rows = (
                    self.db.execute(
                        select(File.id).where(
                            File.workspace_id == workspace_id,
                            File.deleted_at.is_(None),
                        )
                    )
                    .scalars()
                    .all()
                )
                return ResolvedFileScope.finite(rows)

            from openrag.services.workspace_service import WorkspaceService

            ws_service = WorkspaceService(self.db)
            workspaces = ws_service.get_user_workspaces_with_permission(
                user_id, permission="read"
            )
            ws_ids = [ws.id for ws in workspaces]
            if not ws_ids:
                return ResolvedFileScope.finite(())
            rows = (
                self.db.execute(
                    select(File.id).where(
                        File.workspace_id.in_(ws_ids),
                        File.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            return ResolvedFileScope.finite(rows)
        except Exception as exc:
            logger.exception("Could not resolve workspace permissions; failing closed")
            raise PermissionScopeResolutionError(
                "Unable to verify readable file scope"
            ) from exc

    def _filter_to_active_file_ids(self, file_ids: list[int]) -> list[int]:
        """Keep only file_ids whose row is active (deleted_at IS NULL), order preserved.

        Codex round-4 #2: contextual search derives ``candidate_files`` from L0 hits and
        feeds them into L1 retrieval, the LLM navigation summary, chunk vector search, ES
        score blending, and L0/L1 score normalization. A soft-deleted file in that list
        pollutes ALL of those even if its chunks are dropped from the final result. Filter
        the candidate ids up front so soft-deleted files never influence contextual ranking.
        """
        if not file_ids:
            return []
        active = set(
            self.db.execute(
                select(File.id).where(
                    File.id.in_(file_ids),
                    File.deleted_at.is_(None),
                )
            ).scalars().all()
        )
        return [fid for fid in file_ids if fid in active]

    def _filter_hits_to_active_files(self, hits: list[dict]) -> list[dict]:
        """Drop hits whose file is missing or soft-deleted (Codex round-3 #3).

        Belt-and-suspenders for the admin/global path where _accessible_file_ids
        returns None (no id filter): a soft-deleted file's vectors may survive until
        the worker physically cleans them, so filter at result time too.
        """
        file_ids = {h.get("file_id") for h in hits if h.get("file_id") is not None}
        if not file_ids:
            return [h for h in hits if h.get("file_id") is not None]
        active = set(
            self.db.execute(
                select(File.id).where(
                    File.id.in_(file_ids),
                    File.deleted_at.is_(None),
                )
            ).scalars().all()
        )
        return [h for h in hits if h.get("file_id") in active]

    def _effective_file_scope(
        self,
        user_id: int,
        workspace_id: Optional[int],
        scope_file_ids: Optional[set[int]] = None,
    ) -> ResolvedFileScope:
        """Return verified permissions intersected with the requested scope."""
        accessible = self._accessible_file_ids(user_id, workspace_id)
        if not isinstance(accessible, ResolvedFileScope):
            raise PermissionScopeResolutionError(
                "Permission resolver returned an unverified scope"
            )
        if scope_file_ids is None:
            return accessible
        if accessible.is_verified_global:
            return ResolvedFileScope.finite(sorted(scope_file_ids))
        scope = set(scope_file_ids)
        return ResolvedFileScope.finite(
            fid for fid in (accessible.allowed_file_ids or ()) if fid in scope
        )

    def _effective_file_ids(
        self,
        user_id: int,
        workspace_id: Optional[int],
        scope_file_ids: Optional[set[int]] = None,
    ) -> Optional[list[int]]:
        """Compatibility accessor backed by explicit scope resolution."""
        return self._effective_file_scope(
            user_id, workspace_id, scope_file_ids
        ).backend_file_ids()

    def _embed_query(self, query: str) -> list[float]:
        model_name = (
            getattr(self.embedding_engine, "model_name", None)
            or getattr(self.embedding_engine, "model", None)
            or self.embedding_engine.__class__.__name__
        )
        self.trace_service.start_span(
            "retrieval.embed_query",
            input_summary={"embedding_model": str(model_name)},
        )
        try:
            query_vec = self.embedding_engine.embed_text(query)
            try:
                dimension = len(query_vec)
            except TypeError:
                dimension = getattr(self.embedding_engine, "dimension", None)
            self.trace_service.finish_span(
                output_summary={
                    "embedding_model": str(model_name),
                    "dimension": dimension,
                    "success": True,
                }
            )
            return query_vec
        except Exception as exc:
            self.trace_service.fail_span(
                error_message=str(exc),
                metrics={"embedding_model": str(model_name), "success": False},
            )
            raise

    def _record_chunk_search_snapshots(
        self, hits: list[dict], *, stage: str = "retrieval.chunk_search"
    ) -> None:
        for rank, hit in enumerate(hits[:50], start=1):
            self.trace_service.record_snapshot(
                stage=stage,
                rank=rank,
                chunk_id=_safe_chunk_id(hit),
                file_id=_safe_int(hit.get("file_id")),
                score=_safe_float(hit.get("score")),
                score_parts={"vector_score": _safe_float(hit.get("score"))},
                metadata={"phase": "output"},
            )

    def _record_fusion_snapshots(
        self,
        hits: list[dict],
        bm25_scores: Optional[list[float]],
        *,
        phase: str,
    ) -> None:
        for rank, hit in enumerate(hits[:50], start=1):
            bm25_score = (
                bm25_scores[rank - 1]
                if bm25_scores is not None and rank - 1 < len(bm25_scores)
                else _safe_float(hit.get("bm25_score"))
            )
            vector_score = _safe_float(hit.get("fused_score", hit.get("score")))
            fused_score = _safe_float(hit.get("fused_score", vector_score))
            self.trace_service.record_snapshot(
                stage="retrieval.es_fusion",
                rank=rank,
                chunk_id=_safe_chunk_id(hit),
                file_id=_safe_int(hit.get("file_id")),
                score=fused_score,
                score_parts={
                    "vector_score": vector_score,
                    "bm25_score": bm25_score,
                    "fused_score": fused_score,
                },
                metadata={"phase": phase},
            )


def _safe_chunk_id(hit: dict) -> Optional[str]:
    chunk_id = hit.get("chunk_id")
    if chunk_id is None:
        return None
    return str(chunk_id)


def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
