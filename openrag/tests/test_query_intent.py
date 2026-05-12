"""Tests for B5 retrieval strategy inference."""

from openrag.retrieval.query_intent import infer_retrieval_strategy, normalize_strategy


def test_infer_light_short():
    assert infer_retrieval_strategy("增值税率") == "light"


def test_infer_deep_keywords():
    assert infer_retrieval_strategy("请对比A与B的差异") == "deep"
    assert infer_retrieval_strategy("为什么需要备案？") == "deep"


def test_infer_precise_code():
    assert infer_retrieval_strategy("```python\ndef foo():\n  pass\n```") == "precise"
    assert infer_retrieval_strategy("class Bar: pass") == "precise"


def test_normalize_strategy():
    assert normalize_strategy("AUTO") == "auto"
    assert normalize_strategy("light") == "light"
    assert normalize_strategy("bogus") == "auto"
