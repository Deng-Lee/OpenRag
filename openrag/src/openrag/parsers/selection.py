"""Parser selection policy shared by ingest, reprocess, and processing."""

from pathlib import Path


def resolve_pdf_default_parser_type(file_path: str, parser_type: str | None) -> str:
    """Resolve only the PDF-specific default; keep other ``auto`` values unchanged."""
    requested = (parser_type or "auto").strip().lower()
    if requested == "auto" and Path(file_path).suffix.lower() == ".pdf":
        return "pdf"
    return requested
