"""Compatibility module for rag.nlp.rag_tokenizer imports."""

from __future__ import annotations

import re


class RagTokenizer:
    def tokenize(self, text: str) -> str:
        s = (text or "").strip()
        if not s:
            return ""
        tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|[^\s]", s)
        return " ".join(tokens)

    def fine_grained_tokenize(self, tokens: str) -> str:
        return tokens

    def tag(self, token: str) -> str:
        t = (token or "").strip()
        if not t:
            return "x"
        if self.is_chinese(t[0]):
            return "n"
        if t.isdigit():
            return "m"
        return "n"

    def is_chinese(self, ch: str) -> bool:
        return bool(ch) and "\u4e00" <= ch <= "\u9fff"


rag_tokenizer = RagTokenizer()

__all__ = ["RagTokenizer", "rag_tokenizer"]
