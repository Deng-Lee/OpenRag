"""Versioned Elasticsearch chunk document contract for A02."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any
import unicodedata

SCHEMA_VERSION = "a02-content-exact-v2"
QUERY_PROFILE_VERSION = "a02-content-exact-v1"
CONTENT_ANALYZER = "standard"

_PROPERTIES: dict[str, dict[str, Any]] = {
    "chunk_id": {"type": "keyword"},
    "file_id": {"type": "long"},
    "workspace_id": {"type": "long"},
    "workspace_slug": {"type": "keyword"},
    "content": {
        "type": "text",
        "analyzer": CONTENT_ANALYZER,
        "search_analyzer": CONTENT_ANALYZER,
    },
    "doc_type_kwd": {"type": "keyword"},
    "content_with_weight": {"type": "text", "index": False},
    "mom_with_weight": {"type": "text", "index": False},
    "exact_terms": {"type": "keyword"},
    "schema_version": {"type": "keyword"},
    "mapping_hash": {"type": "keyword"},
}

_REQUIRED_FIELDS = {
    "chunk_id",
    "file_id",
    "workspace_id",
    "workspace_slug",
    "content",
    "doc_type_kwd",
    "schema_version",
    "mapping_hash",
}

_MAX_EXACT_TERM_LENGTH = 128
_MAX_EXACT_TERMS = 64
_STANDARD_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z]{1,12}/[A-Za-z]{1,12}\s+"
    r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+"
    r"(?![A-Za-z0-9])"
)
_SEPARATED_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+"
    r"(?![A-Za-z0-9])"
)
_MIXED_ALNUM_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9]{5,}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9]*[A-Za-z])"
    r"(?=[A-Za-z0-9]*[0-9])"
    r"[A-Za-z0-9]+"
    r"(?![A-Za-z0-9])"
)


def _mapping_without_hash() -> dict[str, Any]:
    return {
        "dynamic": "strict",
        "_meta": {
            "schema_version": SCHEMA_VERSION,
            "query_profile_version": QUERY_PROFILE_VERSION,
            "content_analyzer": CONTENT_ANALYZER,
        },
        "properties": deepcopy(_PROPERTIES),
    }


def compute_mapping_hash() -> str:
    """Return a stable hash without including ``_meta.mapping_hash`` itself."""
    canonical = json.dumps(
        _mapping_without_hash(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_chunk_mapping() -> dict[str, Any]:
    """Return a fresh, independently mutable strict v2 mapping."""
    mapping = _mapping_without_hash()
    mapping["_meta"]["mapping_hash"] = compute_mapping_hash()
    return mapping


def normalize_exact_term(value: str) -> str:
    """Normalize width, case and whitespace without removing identifier separators."""
    normalized = unicodedata.normalize("NFKC", str(value)).lower()
    return " ".join(normalized.split())


def extract_exact_terms(text: str) -> list[str]:
    """Extract a conservative, deterministic set of structured identifiers."""
    normalized_text = unicodedata.normalize("NFKC", str(text or ""))
    candidates: list[tuple[int, int, str]] = []
    for pattern in (
        _STANDARD_IDENTIFIER,
        _SEPARATED_IDENTIFIER,
        _MIXED_ALNUM_IDENTIFIER,
    ):
        candidates.extend(
            (match.start(), match.end(), match.group(0))
            for match in pattern.finditer(normalized_text)
        )

    selected_spans: list[tuple[int, int]] = []
    terms: list[str] = []
    seen: set[str] = set()
    for start, end, candidate in sorted(
        candidates, key=lambda item: (item[0], -(item[1] - item[0]))
    ):
        if any(start < chosen_end and end > chosen_start for chosen_start, chosen_end in selected_spans):
            continue
        selected_spans.append((start, end))
        term = normalize_exact_term(candidate)
        if not term or len(term) > _MAX_EXACT_TERM_LENGTH or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= _MAX_EXACT_TERMS:
            break
    return terms


def build_chunk_search_document(
    *,
    chunk_id: str,
    file_id: int,
    workspace_id: int,
    workspace_slug: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the only allowed v2 document shape from one indexed content value."""
    from openrag.search.workspace_es_slug import normalize_workspace_slug_segment

    source_metadata = metadata if isinstance(metadata, dict) else {}
    indexed_content = str(content or "")[:65000]
    doc_type = source_metadata.get("doc_type_kwd")
    if not isinstance(doc_type, str) or not doc_type.strip():
        doc_type = "text"
    mom = source_metadata.get("mom_with_weight")
    if not isinstance(mom, str):
        mom = ""
    document = {
        "chunk_id": str(chunk_id or "")[:64],
        "file_id": int(file_id),
        "workspace_id": int(workspace_id),
        "workspace_slug": normalize_workspace_slug_segment(
            workspace_slug, int(workspace_id)
        ),
        "content": indexed_content,
        "doc_type_kwd": doc_type.strip()[:64],
        "content_with_weight": indexed_content,
        "mom_with_weight": mom[:65000],
        "exact_terms": extract_exact_terms(indexed_content),
        "schema_version": SCHEMA_VERSION,
        "mapping_hash": compute_mapping_hash(),
    }
    validate_chunk_search_document(document)
    return document


def validate_chunk_search_document(document: dict[str, Any]) -> None:
    """Reject documents that do not exactly match the v2 source contract."""
    if not isinstance(document, dict):
        raise ValueError("chunk search document must be an object")

    fields = set(document)
    unknown = fields - set(_PROPERTIES)
    if unknown:
        raise ValueError(f"chunk search document has unknown fields: {sorted(unknown)}")
    missing = _REQUIRED_FIELDS - fields
    if missing:
        raise ValueError(f"chunk search document missing required fields: {sorted(missing)}")

    _require_non_empty_string(document, "chunk_id")
    _require_integer(document, "file_id")
    _require_integer(document, "workspace_id")
    _require_non_empty_string(document, "workspace_slug")
    _require_string(document, "content")
    _require_non_empty_string(document, "doc_type_kwd")

    for field in ("content_with_weight", "mom_with_weight"):
        if field in document:
            _require_string(document, field)

    if "exact_terms" in document:
        terms = document["exact_terms"]
        if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
            raise ValueError("exact_terms must be a list of strings")

    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {document['schema_version']!r}"
        )
    expected_hash = compute_mapping_hash()
    if document["mapping_hash"] != expected_hash:
        raise ValueError(
            f"mapping_hash must be {expected_hash!r}, got {document['mapping_hash']!r}"
        )


def _require_string(document: dict[str, Any], field: str) -> None:
    if not isinstance(document[field], str):
        raise ValueError(f"{field} must be a string")


def _require_non_empty_string(document: dict[str, Any], field: str) -> None:
    _require_string(document, field)
    if not document[field].strip():
        raise ValueError(f"{field} must not be empty")


def _require_integer(document: dict[str, Any], field: str) -> None:
    value = document[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
