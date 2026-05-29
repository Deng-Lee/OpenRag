"""Lightweight schemas for retrieval evaluation metrics."""

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RetrievalResultItem:
    """One ranked retrieval result."""

    chunk_id: str | None = None
    file_id: int | None = None
    score: float | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EvalJudgmentItem:
    """One chunk relevance judgment for an evaluation query."""

    chunk_id: str | None = None
    file_id: int | None = None
    relevance_grade: int = 0
    source: str = "gold_manual"
    weight: float | None = None
    metadata: Mapping[str, Any] | None = None
