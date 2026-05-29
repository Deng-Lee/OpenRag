"""Tests for L1 LLM navigator model endpoint configuration."""

from __future__ import annotations

import sys
import types

from openrag.retrieval.l1_llm_navigator import llm_select_chunk_indices


def test_l1_navigator_prefers_dedicated_key_and_base_url(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["create"] = kwargs
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content='{"selections":[{"file_id":1,"indices":[2]}]}'
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, *, api_key, base_url=None):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "embedding-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setenv("L1_NAV_API_KEY", "l1-key")
    monkeypatch.setenv("L1_NAV_BASE_URL", "https://l1.example/v1")
    monkeypatch.setenv("L1_NAV_MODEL", "deepseek-v4-flash")

    result = llm_select_chunk_indices(
        "find the matching chunk",
        [{"file_id": 1, "name": "doc", "chunk_count": 3, "l1_text": "chunks/0002.md"}],
    )

    assert result.applied is True
    assert result.restrictions == {1: {2}}
    assert captured["api_key"] == "l1-key"
    assert captured["base_url"] == "https://l1.example/v1"
    assert captured["create"]["model"] == "deepseek-v4-flash"
