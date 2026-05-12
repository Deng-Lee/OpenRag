"""B5: 轻量规则推断检索策略（意图路由初版，无 LLM）。"""

from __future__ import annotations

import re

# 返回: light | deep | precise | flat
_DEEP_KEYWORDS = (
    "对比",
    "比较",
    "为什么",
    "为何",
    "分析",
    "总结",
    "归纳",
    "综合",
    "论述",
    "评价",
    "优缺点",
    "多个",
    "哪些",
    "列出所有",
    "详细",
    "深入",
)


def infer_retrieval_strategy(query: str) -> str:
    """
    推断检索策略：
    - light: 短句、偏事实
    - deep: 复杂推理 / 多文档倾向
    - precise: 代码 / 结构化片段
    - flat: 明确要求只搜正文切片（可由 API 显式指定）
    """
    q = (query or "").strip()
    if not q:
        return "deep"

    if "```" in q or re.search(r"\b(def|class|import|function)\b", q):
        return "precise"

    if any(k in q for k in _DEEP_KEYWORDS):
        return "deep"

    if len(q) < 18 and "？" not in q and "?" not in q and "\n" not in q:
        return "light"

    return "deep"


def normalize_strategy(name: str) -> str:
    n = (name or "auto").strip().lower()
    if n in ("light", "deep", "precise", "flat"):
        return n
    return "auto"
