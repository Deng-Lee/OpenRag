"""Minimal compatibility helpers used by RAGFlow parser code."""

from pathlib import Path


def get_project_base_directory() -> str:
    """Return project root (parent of src/) so rag/res paths resolve."""
    return str(Path(__file__).resolve().parents[2])

