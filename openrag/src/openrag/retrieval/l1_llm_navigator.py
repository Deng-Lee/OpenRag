"""B7/B8: 根据 L1 概述与用户问题，由 LLM 选择可能相关的 chunk 序号（0-based）。"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o-mini"
_MAX_L1_CHARS = 6000
_MAX_INDICES_PER_FILE = 16


@dataclass
class L1LlmNavigationResult:
    restrictions: Dict[int, Set[int]] = field(default_factory=dict)
    applied: bool = False
    skip_reason: Optional[str] = None


def _extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if m:
            text = m.group(1).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    return json.loads(text)


def llm_select_chunk_indices(
    user_query: str,
    file_summaries: List[dict],
) -> L1LlmNavigationResult:
    """调用 LLM 根据 L1 概述选择 chunk 下标；返回 restrictions + applied/skip_reason."""
    if not file_summaries:
        return L1LlmNavigationResult(skip_reason="no_l1_text")

    api_key = os.environ.get("L1_NAV_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.debug("L1 LLM navigator skipped: L1_NAV_API_KEY/OPENAI_API_KEY unset")
        return L1LlmNavigationResult(skip_reason="no_api_key")

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package missing; L1 LLM navigator disabled")
        return L1LlmNavigationResult(skip_reason="no_openai_package")

    model = os.environ.get("L1_NAV_MODEL", _DEFAULT_MODEL)
    base_url = os.environ.get("L1_NAV_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or None

    lines = []
    for item in file_summaries:
        fid = item.get("file_id")
        name = item.get("name") or f"file_{fid}"
        nchunks = int(item.get("chunk_count") or 0)
        l1 = (item.get("l1_text") or "")[:_MAX_L1_CHARS]
        lines.append(
            f"### file_id={fid} name={name!r} chunk_count={nchunks}\n"
            f"L1 overview:\n{l1}\n"
        )
    catalog = "\n".join(lines)

    system = (
        "你是检索规划助手。根据用户问题和每个文档的 L1 概述（含 Structure 中 chunks/NNNN.md 线索），"
        "判断为回答问题最可能需要阅读的切片下标（0-based，与 NNNN 四位数字一致，如 0003 -> 3）。"
        "只输出严格 JSON，不要其它文字。"
        '格式: {"selections":[{"file_id":整型,"indices":[整数,...]}]}。'
        "若某文档无法判断，将该文档的 indices 设为 []（表示不限制该文档，由向量检索决定）。"
        f"每个文件 indices 最多 {_MAX_INDICES_PER_FILE} 个。"
    )
    user = f"用户问题：\n{user_query}\n\n---\n文档 L1 目录：\n{catalog}"

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = _extract_json_object(raw)
    except Exception as exc:
        logger.warning("L1 LLM navigator call failed: %s", exc)
        return L1LlmNavigationResult(skip_reason=f"llm_call_failed: {exc}")

    out: Dict[int, Set[int]] = {}
    for sel in data.get("selections", []):
        try:
            fid = int(sel.get("file_id"))
        except (TypeError, ValueError):
            continue
        raw_idx = sel.get("indices") or sel.get("chunk_indices") or []
        if not raw_idx:
            continue
        idx_set: Set[int] = set()
        for x in raw_idx[:_MAX_INDICES_PER_FILE]:
            try:
                idx_set.add(int(x))
            except (TypeError, ValueError):
                continue
        if idx_set:
            out[fid] = idx_set

    logger.info("L1 LLM navigator: restrictions for %d file(s)", len(out))
    return L1LlmNavigationResult(restrictions=out, applied=bool(out))


def _best_l1_text_per_file(l1_hits: List[dict]) -> Dict[int, str]:
    best: Dict[int, tuple[float, str]] = {}
    for h in l1_hits:
        fid_raw = h.get("file_id")
        if fid_raw is None:
            continue
        try:
            fid = int(fid_raw)
        except (TypeError, ValueError):
            continue
        s = float(h.get("score", 0))
        t = str(h.get("text") or "")
        if fid not in best or s > best[fid][0]:
            best[fid] = (s, t)
    return {fid: pair[1] for fid, pair in best.items()}


def filter_chunk_hits_by_indices(
    chunk_hits: List[dict],
    restrictions: Dict[int, Set[int]],
) -> List[dict]:
    """按 LLM 给出的 file_id->indices 过滤；若过滤后为空则返回原列表。"""
    if not restrictions:
        return chunk_hits

    kept: List[dict] = []
    for h in chunk_hits:
        try:
            fid = int(h.get("file_id", 0))
        except (TypeError, ValueError):
            kept.append(h)
            continue
        allowed = restrictions.get(fid)
        if allowed is None:
            kept.append(h)
            continue
        idx = h.get("chunk_index")
        if idx is None:
            kept.append(h)
            continue
        try:
            if int(idx) in allowed:
                kept.append(h)
        except (TypeError, ValueError):
            kept.append(h)

    if not kept:
        logger.info("L1 index filter removed all hits; keeping original vector results")
        return chunk_hits
    return kept
