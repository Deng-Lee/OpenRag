"""Reranker for improving search result ranking using cross-encoder and hierarchical information"""

import logging
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

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

    def _ensure_model(self):
        """Lazy-load the cross-encoder model on first use."""
        if self._model is None:
            self._model = _get_cross_encoder_model(self.model_name)

    def rerank(self, query: str, results: list[dict], top_k: int = 10) -> list[dict]:
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

        # Compute reranked scores for each result
        cross_scores = self._batch_cross_encoder_scores(query, results)

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
        return reranked_results[:top_k]

    # ------------------------------------------------------------------
    # Cross-encoder scoring: real model with keyword fallback
    # ------------------------------------------------------------------

    def _batch_cross_encoder_scores(
        self, query: str, results: list[dict]
    ) -> list[float]:
        """Score all query-text pairs in one batch call; fallback to keyword overlap."""
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
                expanded_result["parent_context"] = {
                    "text": parent_chunk.text_preview or "",
                    "level": parent_chunk.level,
                    "block_type": parent_chunk.block_type,
                }
            else:
                expanded_result["parent_context"] = None
        except Exception as exc:
            logger.warning("expand_context failed: %s", exc)
            expanded_result["parent_context"] = None

        return expanded_result
