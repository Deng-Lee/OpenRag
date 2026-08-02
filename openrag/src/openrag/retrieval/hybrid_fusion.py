"""Pure candidate fusion for independent Dense and Sparse recall."""

from __future__ import annotations

from typing import Optional


def _ranked_unique(hits: list[dict]) -> list[tuple[int, str, dict]]:
    ranked: list[tuple[int, str, dict]] = []
    seen: set[str] = set()
    for rank, hit in enumerate(hits, start=1):
        chunk_id = hit.get("chunk_id")
        if chunk_id is None:
            continue
        key = str(chunk_id)
        if not key or key in seen:
            continue
        seen.add(key)
        ranked.append((rank, key, hit))
    return ranked


def weighted_rrf(
    dense_hits: list[dict],
    sparse_hits: list[dict],
    *,
    dense_weight: float,
    rrf_k: int = 60,
    limit: Optional[int] = None,
) -> list[dict]:
    """Fuse ranked hit lists by chunk_id using deterministic Weighted RRF."""
    weight = float(dense_weight)
    if not 0.0 <= weight <= 1.0:
        raise ValueError("dense_weight must be between 0 and 1")
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")
    if limit is not None and limit <= 0:
        return []

    candidates: dict[str, dict] = {}
    if weight > 0.0:
        for rank, chunk_id, hit in _ranked_unique(dense_hits):
            candidate = dict(hit)
            candidate["chunk_id"] = chunk_id
            candidate["dense_score"] = hit.get("dense_score", hit.get("score"))
            candidate["dense_rank"] = rank
            candidate["sparse_rank"] = None
            candidate["sparse_score"] = None
            candidate["recall_sources"] = ["dense"]
            candidates[chunk_id] = candidate

    sparse_weight = 1.0 - weight
    if sparse_weight > 0.0:
        for rank, chunk_id, hit in _ranked_unique(sparse_hits):
            candidate = candidates.get(chunk_id)
            if candidate is None:
                candidate = dict(hit)
                candidate["chunk_id"] = chunk_id
                candidate["dense_score"] = None
                candidate["dense_rank"] = None
                candidate["recall_sources"] = []
                candidates[chunk_id] = candidate
            else:
                for key, value in hit.items():
                    candidate.setdefault(key, value)
            candidate["sparse_score"] = hit.get("sparse_score")
            candidate["sparse_rank"] = rank
            candidate["recall_sources"] = [
                *candidate["recall_sources"],
                "sparse",
            ]

    for candidate in candidates.values():
        dense_rank = candidate["dense_rank"]
        sparse_rank = candidate["sparse_rank"]
        candidate["fused_score"] = (
            weight / (rrf_k + dense_rank) if dense_rank is not None else 0.0
        ) + (
            sparse_weight / (rrf_k + sparse_rank)
            if sparse_rank is not None
            else 0.0
        )

    fused = sorted(
        candidates.values(),
        key=lambda hit: (
            -float(hit["fused_score"]),
            min(
                rank
                for rank in (hit["dense_rank"], hit["sparse_rank"])
                if rank is not None
            ),
            hit["chunk_id"],
        ),
    )
    return fused if limit is None else fused[:limit]
