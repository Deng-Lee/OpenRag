"""RAGFlow-compatible chunk metadata (token fields, positions)."""

from __future__ import annotations

import re
from typing import Any

try:
    from rag.nlp import rag_tokenizer as _ragflow_tokenizer  # type: ignore
except Exception:  # pragma: no cover
    _ragflow_tokenizer = None


def _safe_fine_grained_tokenize(tokenizer, content_ltks: str) -> str:
    """Call fine_grained_tokenize with name-variant fallback for older infinity versions."""
    for attr in ("fine_grained_tokenize", "find_grained_tokenize"):
        fn = getattr(tokenizer, attr, None)
        if fn is not None:
            return fn(content_ltks)
    return content_ltks


def ragflow_tokenize_fields(text: str) -> dict[str, Any]:
    """Build RagFlow-compatible token fields for a chunk text."""
    content_with_weight = text
    normalized = re.sub(
        r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>",
        " ",
        text,
    )
    if _ragflow_tokenizer is not None:
        content_ltks = _ragflow_tokenizer.tokenize(normalized)
        content_sm_ltks = _safe_fine_grained_tokenize(_ragflow_tokenizer, content_ltks)
    else:
        tokens = re.findall(r"[\w]+|[^\w\s]", normalized, re.UNICODE)
        content_ltks = " ".join(tokens)
        content_sm_ltks = content_ltks
    return {
        "content_with_weight": content_with_weight,
        "content_ltks": content_ltks,
        "content_sm_ltks": content_sm_ltks,
    }


def ragflow_add_positions_fields(
    poss: list[tuple[float, float, float, float, float]],
) -> dict[str, Any]:
    """Build RagFlow-compatible add_positions fields."""
    if not poss:
        return {}
    page_num_int: list[int] = []
    position_int: list[tuple[int, int, int, int, int]] = []
    top_int: list[int] = []
    for pn, left, right, top, bottom in poss:
        page_num_int.append(int(pn + 1))
        top_int.append(int(top))
        position_int.append((int(pn + 1), int(left), int(right), int(top), int(bottom)))
    return {
        "page_num_int": page_num_int,
        "position_int": position_int,
        "top_int": top_int,
    }


def build_chunk_metadata(
    text: str,
    ck_type: str,
    positions: list[tuple[float, float, float, float, float]],
) -> dict[str, Any]:
    """Single merge point for token fields + ``doc_type_kwd`` + position_int family."""
    out: dict[str, Any] = dict(ragflow_tokenize_fields(text))
    out["doc_type_kwd"] = ck_type
    out.update(ragflow_add_positions_fields(positions))
    return out
