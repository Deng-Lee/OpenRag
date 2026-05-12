"""
Utility functions for hierarchy generation.

Adapted from OpenViking (https://github.com/volcengine/openviking)
License: AGPL-3.0
"""

import re
from typing import List


def extract_key_sentences(text: str, max_sentences: int = 5) -> List[str]:
    """Extract key sentences from text.

    Simple heuristic: take first sentence of each paragraph.

    Args:
        text: Input text
        max_sentences: Maximum number of sentences to extract

    Returns:
        List of key sentences
    """
    # Split by paragraphs
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    sentences = []
    for para in paragraphs:
        # Get first sentence of paragraph
        first_sentence = re.split(r'[.!?]\s+', para)[0]
        if first_sentence:
            sentences.append(first_sentence + '.')

        if len(sentences) >= max_sentences:
            break

    return sentences


def count_tokens(text: str) -> int:
    """Estimate token count.

    Simple approximation: 1 token ≈ 4 characters for Chinese/English mix.

    Args:
        text: Input text

    Returns:
        Estimated token count
    """
    return len(text) // 4


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to maximum token count.

    Args:
        text: Input text
        max_tokens: Maximum tokens

    Returns:
        Truncated text
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text

    # Truncate at sentence boundary
    truncated = text[:max_chars]
    last_period = truncated.rfind('.')
    if last_period > 0:
        truncated = truncated[:last_period + 1]

    return truncated


def clean_text(text: str) -> str:
    """Clean text by removing extra whitespace.

    Args:
        text: Input text

    Returns:
        Cleaned text
    """
    # Replace multiple spaces with single space
    text = re.sub(r' +', ' ', text)

    # Replace multiple newlines with single newline
    text = re.sub(r'\n\n+', '\n', text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text
