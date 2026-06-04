"""Document type normalization for chunking pipeline."""

SUPPORTED_DOCUMENT_TYPES = ("general", "manual", "laws")
DEFAULT_DOCUMENT_TYPE = "general"


def normalize_document_type(value: object) -> str:
    """Normalize document_type and reject unsupported values."""
    if value is None:
        return DEFAULT_DOCUMENT_TYPE
    normalized = str(value).strip().lower()
    if not normalized:
        return DEFAULT_DOCUMENT_TYPE
    if normalized not in SUPPORTED_DOCUMENT_TYPES:
        raise ValueError(
            "Invalid document_type. Supported types: "
            f"{', '.join(SUPPORTED_DOCUMENT_TYPES)}"
        )
    return normalized
