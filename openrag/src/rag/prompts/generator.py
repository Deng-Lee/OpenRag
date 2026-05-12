"""Prompt compatibility helpers for RAGFlow parser."""


def vision_llm_describe_prompt(page: int) -> str:
    return f"Please summarize key visual content from page {page}."

