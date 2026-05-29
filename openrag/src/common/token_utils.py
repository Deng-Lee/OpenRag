"""Compatibility shim for common.token_utils - token counting for offline workers."""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _get_encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _fallback_tokens(string: str) -> list[str]:
    return string.split()


def num_tokens_from_string(string: str) -> int:
    """Returns the number of tokens in a text string."""
    encoder = _get_encoder()
    if encoder is None:
        return len(_fallback_tokens(string))
    return len(encoder.encode(string))


def truncate(string: str, max_len: int) -> str:
    """Returns truncated text if the length of text exceed max_len."""
    encoder = _get_encoder()
    if encoder is None:
        return " ".join(_fallback_tokens(string)[:max_len])
    return encoder.decode(encoder.encode(string)[:max_len])


def total_token_count_from_response(resp):
    """Extract token count from LLM response in various formats."""
    if resp is None:
        return 0

    try:
        if hasattr(resp, "usage") and hasattr(resp.usage, "total_tokens"):
            return resp.usage.total_tokens
    except Exception:
        pass

    try:
        if hasattr(resp, "usage_metadata") and hasattr(resp.usage_metadata, "total_tokens"):
            return resp.usage_metadata.total_tokens
    except Exception:
        pass

    if isinstance(resp, dict) and "usage" in resp and "total_tokens" in resp["usage"]:
        try:
            return resp["usage"]["total_tokens"]
        except Exception:
            pass

    return 0
