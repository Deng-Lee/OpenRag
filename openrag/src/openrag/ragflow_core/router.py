"""Extension -> RAGFlow parser strategy name (logical key, not Python import path)."""

from pathlib import Path

_STRATEGY_BY_EXT: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
    ".xlsx": "excel",
    ".xls": "excel",
    ".csv": "excel",
    ".pptx": "ppt",
    ".ppt": "ppt",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".html": "html",
    ".htm": "html",
    ".json": "json",
    ".jsonl": "json",
    ".ldjson": "json",
    ".epub": "epub",
    ".txt": "text",
    ".py": "text",
    ".js": "text",
}


def resolve_parser_strategy(file_path: str) -> str:
    """Return strategy key for *file_path* (basename extension only)."""
    ext = Path(file_path).suffix.lower()
    if ext not in _STRATEGY_BY_EXT:
        raise ValueError(f"Unsupported extension: {ext}")
    return _STRATEGY_BY_EXT[ext]
