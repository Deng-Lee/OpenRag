"""Data models for hierarchy structures."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Section:
    """Represents a section in L1 overview."""
    title: str
    level: int  # Heading level (1, 2, 3)
    content: str  # Preview content
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class HierarchyResult:
    """Result of document hierarchy generation (OpenViking-style layers).

    l0: Abstract (~100 tokens), for retrieval / quick filtering; derived from l1.
    l1: Overview markdown (~1k–2k tokens), for rerank / navigation; from full text.
    l2: Chunk list from ChunkEngine; persisted under chunks/*.md on disk.
    """

    l0: str
    l1: str
    l2: List

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "l0": self.l0,
            "l1": self.l1,
            "l2": [],
        }


@dataclass
class DirectoryHierarchy:
    """Result of directory aggregation."""
    l0: str  # Directory summary
    l1: str  # Directory overview (list of children)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            'l0': self.l0,
            'l1': self.l1
        }
