"""Reranker for improving search result ranking using cross-encoder and hierarchical information"""

import logging
import os
from typing import Optional

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.services.trace_service import TraceService

logger = logging.getLogger(__name__)

_DASHSCOPE_RERANK_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
_API_RERANK_PROVIDERS = {"api", "dashscope", "dashscope_vl", "http"}

# ---------------------------------------------------------------------------
# Lazy-loaded CrossEncoder model (sentence-transformers)
# ---------------------------------------------------------------------------
_model_instance = None
_model_load_attempted = False


def _get_cross_encoder_model(model_name: str):
    """Lazy-load a sentence-transformers CrossEncoder; returns None on failure."""
    global _model_instance, _model_load_attempted

    if _model_load_attempted:
        return _model_instance

    _model_load_attempted = True

    # Allow override via environment variable
    effective_model = os.environ.get("RERANKER_MODEL", model_name)

    try:
        from sentence_transformers import CrossEncoder

        logger.info("Loading CrossEncoder model: %s", effective_model)
        _model_instance = CrossEncoder(effective_model, max_length=512)
        logger.info("CrossEncoder model loaded successfully: %s", effective_model)
        return _model_instance
    except ImportError:
        logger.warning(
            "sentence_transformers not installed; reranker will fall back to "
            "keyword-overlap scoring. Install with: pip install sentence-transformers"
        )
        return None
    except Exception as exc:
        logger.warning(
            "Failed to load CrossEncoder model '%s': %s", effective_model, exc
        )
        return None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class DashScopeRerankAdapter:
    """DashScope HTTP adapter for qwen3-vl-rerank / qwen3-rerank."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        max_documents: Optional[int] = None,
        payload_format: Optional[str] = None,
    ):
        self.model = model or os.environ.get("RERANKER_MODEL", "qwen3-vl-rerank")
        self.api_key = (
            api_key
            or os.environ.get("RERANKER_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self.base_url = (
            base_url or os.environ.get("RERANKER_BASE_URL") or _DASHSCOPE_RERANK_URL
        )
        self.timeout = timeout if timeout is not None else _env_float("RERANKER_TIMEOUT", 30.0)
        self.payload_format = (
            payload_format
            or os.environ.get("RERANKER_PAYLOAD_FORMAT")
            or ("vl" if "vl" in self.model.lower() else "text")
        ).lower()
        default_max = 100 if self.payload_format == "vl" else 500
        self.max_documents = max_documents or _env_int("RERANKER_MAX_DOCUMENTS", default_max)
        self.return_documents = _env_bool("RERANKER_RETURN_DOCUMENTS", False)
        self.instruct = os.environ.get("RERANKER_INSTRUCT")

    def score(self, query: str, results: list[dict]) -> list[float]:
        """Return relevance scores aligned with the input result order."""
        if not results:
            return []
        if not self.api_key:
            raise RuntimeError("RERANKER_API_KEY/DASHSCOPE_API_KEY/OPENAI_API_KEY is not set")

        docs = results[: max(1, self.max_documents)]
        payload = self._build_payload(query, docs)
        response = requests.post(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        ranked = self._extract_ranked_results(data)

        scores = [0.0] * len(results)
        for item in ranked:
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(docs):
                continue
            score = item.get("relevance_score", item.get("score", 0.0))
            try:
                scores[idx] = float(score)
            except (TypeError, ValueError):
                scores[idx] = 0.0
        return scores

    def _build_payload(self, query: str, docs: list[dict]) -> dict:
        texts = [str(d.get("text", "") or "") for d in docs]
        top_n = len(texts)
        parameters = {
            "return_documents": self.return_documents,
            "top_n": top_n,
        }
        if self.instruct and self.payload_format != "vl":
            parameters["instruct"] = self.instruct

        if self.payload_format == "vl":
            input_data = {
                "query": {"text": query},
                "documents": [{"text": text} for text in texts],
            }
        else:
            input_data = {
                "query": query,
                "documents": texts,
            }

        return {
            "model": self.model,
            "input": input_data,
            "parameters": parameters,
        }

    @staticmethod
    def _extract_ranked_results(data: dict) -> list[dict]:
        if not isinstance(data, dict):
            return []
        output = data.get("output")
        if isinstance(output, dict) and isinstance(output.get("results"), list):
            return output["results"]
        if isinstance(data.get("results"), list):
            return data["results"]
        return []


class Reranker:
    """
    Reranker that uses cross-encoder models and hierarchical information to improve ranking.

    This reranker:
    - Uses real cross-encoder scoring via sentence-transformers (with keyword-overlap fallback)
    - Applies hierarchical boosts (titles, headings get higher scores)
    - Applies position boosts (early pages, larger elements get higher scores)
    - Supports context expansion using parent chunks
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        hierarchical_boost: float = 0.1,
        position_boost: float = 0.05,
    ):
        """
        Initialize reranker with model and boost factors.

        Args:
            model_name: Cross-encoder model name for sentence-transformers
            hierarchical_boost: Boost factor for hierarchical elements (0.0-1.0)
            position_boost: Boost factor for position-based scoring (0.0-1.0)
        """
        self.model_name = model_name
        self.hierarchical_boost = hierarchical_boost
        self.position_boost = position_boost
        self._model = None
        self.provider = os.environ.get("RERANKER_PROVIDER", "local").strip().lower()
        api_model = os.environ.get("RERANKER_MODEL") or "qwen3-vl-rerank"
        self._api_adapter = (
            DashScopeRerankAdapter(model=api_model)
            if self.provider in _API_RERANK_PROVIDERS
            else None
        )

    @property
    def model(self):
        """Backward-compatible access to the lazily loaded local model."""
        return self._model

    def _ensure_model(self):
        """Lazy-load the cross-encoder model on first use."""
        if self._model is None:
            self._model = _get_cross_encoder_model(self.model_name)

    def rerank(
        self,
        query: str,
        results: list[dict],
        top_k: int = 10,
        trace_service: Optional[TraceService] = None,
    ) -> list[dict]:
        """
        Rerank results using cross-encoder and hierarchical/position boosts.

        Args:
            query: Search query
            results: List of search results from RetrievalService
            top_k: Number of top results to return

        Returns:
            Reranked results with updated scores, sorted by reranked_score descending
        """
        if not results:
            return []

        trace_service = trace_service or _NullTraceService()
        trace_service.start_span(
            "retrieval.rerank",
            input_summary={
                "model": self.model_name,
                "provider": self.provider,
                "candidate_count": len(results),
                "top_k": top_k,
            },
        )
        _record_rerank_snapshots(trace_service, results, phase="input")

        # Compute reranked scores for each result
        try:
            cross_scores = self._batch_cross_encoder_scores(query, results)
        except Exception as exc:
            trace_service.fail_span(error_message=str(exc))
            raise

        reranked_results = []
        for i, result in enumerate(results):
            cross_encoder_score = cross_scores[i]

            # Combine with original score (weighted average)
            original_score = result.get("score", 0.0)
            base_score = cross_encoder_score * 0.6 + original_score * 0.4

            # Apply hierarchical boost
            level = result.get("level")
            block_type = result.get("block_type")
            score_with_hierarchy = self._apply_hierarchical_boost(
                base_score, level, block_type
            )

            # Apply position boost
            bbox = result.get("bbox")
            page = result.get("page")
            final_score = self._apply_position_boost(score_with_hierarchy, bbox, page)

            # Create new result with reranked score
            reranked_result = result.copy()
            reranked_result["reranked_score"] = final_score
            reranked_results.append(reranked_result)

        # Sort by reranked_score descending
        reranked_results.sort(key=lambda x: x["reranked_score"], reverse=True)

        # Return top_k results
        final_results = reranked_results[:top_k]
        _record_rerank_snapshots(
            trace_service,
            final_results,
            phase="output",
            original_rank_by_chunk_id=_original_rank_by_chunk_id(results),
        )
        trace_service.finish_span(output_summary={"result_count": len(final_results)})
        return final_results

    # ------------------------------------------------------------------
    # Cross-encoder scoring: real model with keyword fallback
    # ------------------------------------------------------------------

    def _batch_cross_encoder_scores(
        self, query: str, results: list[dict]
    ) -> list[float]:
        """Score all query-text pairs in one batch call; fallback to keyword overlap."""
        if self._api_adapter is not None:
            try:
                scores = self._api_adapter.score(query, results)
                logger.info(
                    "reranker api provider=%s model=%s batch=%d",
                    self.provider,
                    self._api_adapter.model,
                    len(results),
                )
                return scores
            except Exception as exc:
                logger.warning("API reranker failed, falling back to local reranker: %s", exc)

        self._ensure_model()

        if self._model is not None:
            try:
                pairs = [(query, r.get("text", "")) for r in results]
                raw_scores = self._model.predict(pairs)

                # CrossEncoder raw scores can be negative; normalise to [0, 1]
                smin = float(min(raw_scores))
                smax = float(max(raw_scores))
                if smax - smin < 1e-9:
                    norm = [0.5] * len(raw_scores)
                else:
                    norm = [(float(s) - smin) / (smax - smin) for s in raw_scores]
                logger.info(
                    "reranker cross_encoder batch=%d raw_min=%.4f raw_max=%.4f",
                    len(pairs),
                    smin,
                    smax,
                )
                return norm
            except Exception as exc:
                logger.warning(
                    "CrossEncoder predict failed, falling back to keyword overlap: %s",
                    exc,
                )

        # Fallback: keyword overlap (original mock logic)
        return [self._keyword_overlap_score(query, r.get("text", "")) for r in results]

    def _compute_cross_encoder_score(self, query: str, text: str) -> float:
        """Compatibility helper for single-pair tests and callers."""
        return self._batch_cross_encoder_scores(query, [{"text": text}])[0]

    @staticmethod
    def _keyword_overlap_score(query: str, text: str) -> float:
        """Simple keyword overlap fallback when CrossEncoder is unavailable."""
        if not query or not text:
            return 0.0

        query_words = set(query.lower().split())
        text_words = set(text.lower().split())

        if not query_words:
            return 0.0

        overlap = len(query_words & text_words)
        return min(1.0, max(0.0, overlap / len(query_words)))

    # ------------------------------------------------------------------
    # Hierarchical & position boosts (unchanged logic)
    # ------------------------------------------------------------------

    def _apply_hierarchical_boost(
        self,
        score: float,
        level: Optional[int],
        block_type: Optional[str],
    ) -> float:
        """Apply hierarchical boost based on element level and type."""
        if level is None and block_type is None:
            return score

        boost = 0.0

        if block_type == "title":
            boost = self.hierarchical_boost * 1.0
        elif block_type == "heading" and level is not None:
            if level == 1:
                boost = self.hierarchical_boost * 0.8
            elif level == 2:
                boost = self.hierarchical_boost * 0.6
            elif level == 3:
                boost = self.hierarchical_boost * 0.4
            else:
                boost = self.hierarchical_boost * 0.2

        return score + boost

    def _apply_position_boost(
        self,
        score: float,
        bbox: Optional[list],
        page: Optional[int],
    ) -> float:
        """Apply position boost based on page number and bounding box size."""
        boost = 0.0

        if page is not None:
            page_factor = 1.0 / (1.0 + (page - 1) * 0.2)
            boost += self.position_boost * page_factor * 0.5

        if bbox is not None and len(bbox) == 4:
            x1, y1, x2, y2 = bbox
            area = abs(x2 - x1) * abs(y2 - y1)
            if area > 48000:
                size_factor = min(1.0, area / 240000)
                boost += self.position_boost * size_factor * 0.5

        return score + boost

    def expand_context(self, result: dict, db: Session) -> dict:
        """Expand result with parent chunk context."""
        expanded_result = result.copy()

        parent_chunk_id = result.get("parent_chunk_id")
        if parent_chunk_id is None:
            expanded_result["parent_context"] = None
            return expanded_result

        try:
            from openrag.models.document_chunk import DocumentChunk

            parent_chunk = db.execute(
                select(DocumentChunk).where(DocumentChunk.chunk_id == parent_chunk_id)
            ).scalar_one_or_none()

            if parent_chunk:
                text_preview = getattr(parent_chunk, "text_preview", None)
                if not isinstance(text_preview, str):
                    text_preview = getattr(parent_chunk, "text", "") or ""
                expanded_result["parent_context"] = {
                    "text": text_preview,
                    "level": parent_chunk.level,
                    "block_type": parent_chunk.block_type,
                }
            else:
                expanded_result["parent_context"] = None
        except Exception as exc:
            logger.warning("expand_context failed: %s", exc)
            expanded_result["parent_context"] = None

        return expanded_result


class _NullTraceService:
    def start_span(self, *args, **kwargs):
        return None

    def finish_span(self, *args, **kwargs):
        return None

    def fail_span(self, *args, **kwargs):
        return None

    def record_snapshot(self, *args, **kwargs):
        return None


def _record_rerank_snapshots(
    trace_service,
    results: list[dict],
    *,
    phase: str,
    original_rank_by_chunk_id: Optional[dict[str, int]] = None,
) -> None:
    for rank, result in enumerate(results[:50], start=1):
        chunk_id = result.get("chunk_id")
        chunk_key = str(chunk_id) if chunk_id is not None else None
        original_rank = (
            original_rank_by_chunk_id.get(chunk_key, rank)
            if original_rank_by_chunk_id is not None and chunk_key is not None
            else rank
        )
        rerank_score = result.get("reranked_score")
        trace_service.record_snapshot(
            stage="retrieval.rerank",
            rank=rank,
            chunk_id=chunk_key,
            file_id=_safe_int(result.get("file_id")),
            score=_safe_float(rerank_score),
            score_parts={
                "fused_score": _safe_float(result.get("fused_score", result.get("score"))),
                "rerank_score": _safe_float(rerank_score),
            },
            metadata={
                "phase": phase,
                "original_rank": original_rank,
                "rank_delta": original_rank - rank,
            },
        )


def _original_rank_by_chunk_id(results: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for rank, result in enumerate(results, start=1):
        chunk_id = result.get("chunk_id")
        if chunk_id is not None:
            out[str(chunk_id)] = rank
    return out


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
