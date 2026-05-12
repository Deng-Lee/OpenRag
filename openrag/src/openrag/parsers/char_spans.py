"""整文档「字符流」内段落/子块定位（与 DocumentBlock.char_start/char_end 对齐）。"""

from __future__ import annotations

from typing import Optional


def paragraph_absolute_spans(block_text: str, block_char_start: Optional[int]) -> list[tuple[int, int]]:
    """将 block.text 按 ``\\n\\n`` 分段，返回每段 strip 后在**整文件字符流**中的 [start, end)。

    ``block_char_start`` 为该块在整文件中的起始下标；若为 None 则返回空列表（由上层回退到块内 offset 逻辑）。
    """
    if block_char_start is None:
        return []
    parts = block_text.split("\n\n")
    spans: list[tuple[int, int]] = []
    offset = 0
    for i, para in enumerate(parts):
        stripped = para.strip()
        if stripped:
            rel = para.find(stripped)
            if rel < 0:
                rel = 0
            a = block_char_start + offset + rel
            b = a + len(stripped)
            spans.append((a, b))
        offset += len(para)
        if i + 1 < len(parts):
            offset += 2
    return spans


def paragraph_blocks_from_plaintext(
    raw: str,
    *,
    id_prefix: str,
    split: str = "\n\n",
) -> list:
    """按分隔符切分纯文本，生成带 char_start/char_end/block_id 的 DocumentBlock 列表。"""
    from openrag.parsers.base import DocumentBlock

    if split == "\n\n":
        parts = raw.split("\n\n")
    else:
        parts = raw.split(split)

    blocks: list[DocumentBlock] = []
    idx = 0
    offset = 0
    for i, para in enumerate(parts):
        stripped = para.strip()
        if stripped:
            rel = para.find(stripped)
            if rel < 0:
                rel = 0
            start = offset + rel
            end = start + len(stripped)
            blocks.append(
                DocumentBlock(
                    text=stripped,
                    page=1,
                    offset=idx,
                    block_type="text",
                    level=0,
                    layout_type="text",
                    block_id=f"{id_prefix}:p:{idx}",
                    char_start=start,
                    char_end=end,
                )
            )
            idx += 1
        offset += len(para)
        if i + 1 < len(parts):
            offset += len(split)
    return blocks
