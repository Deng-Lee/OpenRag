"""RAGFlow-style semantic chunking (naive merge, docx-like blocks, children delimiters)."""

from __future__ import annotations

import bisect
import os
import re
import uuid
from collections.abc import Callable
from typing import Any, Optional

try:
    from common.token_utils import num_tokens_from_string
except ImportError:
    def num_tokens_from_string(text: str) -> int:
        """Fallback token counter when common.token_utils is unavailable."""
        return max(1, len(text) // 4)

from openrag.chunking.chunk_models import Chunk
from openrag.chunking.ragflow_core.metadata import (
    ragflow_add_positions_fields,
    ragflow_tokenize_fields,
)
from openrag.parsers.base import DocumentBlock


def remove_position_tags(text: str) -> str:
    """Remove RAGFlow-style position tags from text."""
    text = re.sub(
        r"@@([0-9]+(?:-[0-9]+)?)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)##",
        "",
        text,
    )
    return re.sub(r"@@[\t0-9.-]+?##", "", text)


def _merge_short_tail(
    texts: list[str], tk_nums: list[int], min_chunk_tokens: int
) -> tuple[list[str], list[int]]:
    """Merge trailing short chunks into their predecessor until min_chunk_tokens met."""
    if not texts or min_chunk_tokens <= 0:
        return texts, tk_nums
    out_texts = list(texts)
    out_tks = list(tk_nums)
    while len(out_texts) >= 2 and out_tks[-1] < min_chunk_tokens:
        out_texts[-2] += out_texts[-1]
        out_tks[-2] += out_tks[-1]
        out_texts.pop()
        out_tks.pop()
    return out_texts, out_tks


def ragflow_naive_merge(
    sections: list[tuple[str, str]] | list[str] | str,
    chunk_token_num: int = 128,
    delimiter: str = "\n!?;。；！？",
    overlapped_percent: int = 0,
    strict_limit: bool = False,
    min_chunk_tokens: int = 0,
) -> list[str]:
    """RAGFlow-style naive merge (text-only variant).

    *min_chunk_tokens*: when > 0, the last chunk is forced to stay open
    (i.e. incoming sections keep being appended) until its token count reaches
    this threshold — prevents half-sentence fragments.
    """
    if not sections:
        return []
    if isinstance(sections, str):
        sections = [sections]
    if isinstance(sections[0], str):
        sections = [(s, "") for s in sections]  # type: ignore[assignment,index]

    cks = [""]
    tk_nums = [0]
    limit = chunk_token_num * (100 - overlapped_percent) / 100.0

    def add_chunk(t: str, pos: str):
        nonlocal cks, tk_nums
        tnum = num_tokens_from_string(t)
        if not pos:
            pos = ""
        if tnum < 8:
            pos = ""

        prev_below_min = (
            min_chunk_tokens > 0
            and tk_nums[-1] < min_chunk_tokens
            and cks[-1] != ""
        )

        should_start_new = (cks[-1] == "" or tk_nums[-1] > limit) and not prev_below_min
        if strict_limit and cks[-1] != "" and (tk_nums[-1] + tnum) > limit and not prev_below_min:
            should_start_new = True

        if should_start_new:
            if cks:
                overlapped = remove_position_tags(cks[-1])
                t = overlapped[int(len(overlapped) * (100 - overlapped_percent) / 100.0) :] + t
            if t.find(pos) < 0:
                t += pos
            cks.append(t)
            tk_nums.append(tnum)
        else:
            if cks[-1].find(pos) < 0:
                t += pos
            cks[-1] += t
            tk_nums[-1] += tnum

    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    if has_custom:
        custom_pattern = "|".join(
            re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True)
        )
        cks, tk_nums = [], []
        for sec, pos in sections:  # type: ignore[misc]
            split_sec = re.split(r"(%s)" % custom_pattern, sec, flags=re.DOTALL)
            for sub_sec in split_sec:
                if re.fullmatch(custom_pattern, sub_sec or ""):
                    continue
                text = "\n" + sub_sec
                local_pos = pos
                if num_tokens_from_string(text) < 8:
                    local_pos = ""
                if local_pos and text.find(local_pos) < 0:
                    text += local_pos
                cks.append(text)
                tk_nums.append(num_tokens_from_string(text))
        # post-merge short chunks in custom-delimiter path
        if min_chunk_tokens > 0:
            cks, tk_nums = _merge_short_tail(cks, tk_nums, min_chunk_tokens)
        return cks

    for sec, pos in sections:  # type: ignore[misc]
        add_chunk("\n" + sec, pos)

    if min_chunk_tokens > 0 and len(cks) >= 2 and tk_nums[-1] < min_chunk_tokens:
        cks[-2] += cks[-1]
        tk_nums[-2] += tk_nums[-1]
        cks.pop()
        tk_nums.pop()

    return cks


def split_with_pattern(content: str, pattern: str) -> list[str]:
    """Split content with RAGFlow-like child delimiters behavior."""
    if not pattern:
        return [content]
    try:
        compiled_pattern = re.compile(r"(%s)" % pattern, flags=re.DOTALL)
    except re.error:
        return [content]

    docs: list[str] = []
    txts = [txt for txt in compiled_pattern.split(content)]
    for j in range(0, len(txts), 2):
        txt = txts[j]
        if not txt:
            continue
        if j + 1 < len(txts):
            txt += txts[j + 1]
        docs.append(txt)
    return docs or [content]


def _normalize_spaces_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize whitespace and keep normalized-index -> original-index map."""
    out: list[str] = []
    idx_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx_map.append(i)
            prev_space = True
        else:
            out.append(ch)
            idx_map.append(i)
            prev_space = False
    return "".join(out), idx_map


def find_chunk_pos_robust(full_text: str, chunk_text: str, search_from: int) -> int:
    """Find chunk start with exact-first, whitespace-normalized fallback."""
    pos = full_text.find(chunk_text, search_from)
    if pos >= 0:
        return pos
    pos = full_text.find(chunk_text)
    if pos >= 0:
        return pos

    n_full, full_map = _normalize_spaces_with_map(full_text)
    n_chunk, _ = _normalize_spaces_with_map(chunk_text)
    if not n_chunk:
        return -1

    norm_search = bisect.bisect_left(full_map, max(0, search_from))
    n_pos = n_full.find(n_chunk, norm_search)
    if n_pos < 0:
        n_pos = n_full.find(n_chunk)
    if n_pos < 0 or n_pos >= len(full_map):
        return -1
    return full_map[n_pos]


def _overlap_len(a0: int, a1: int, b0: int, b1: int) -> int:
    """Return overlap length of [a0,a1) and [b0,b1)."""
    return max(0, min(a1, b1) - max(a0, b0))


def build_docx_like_cks(
    sections: list[tuple[str, Optional[bytes], Optional[str]]], delimiter: str
) -> tuple[list[dict[str, Any]], bool]:
    """Build RAGFlow docx-like chunks from text/table/image sections."""
    cks: list[dict[str, Any]] = []
    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    custom_pattern = ""
    if has_custom:
        custom_pattern = "|".join(
            re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True)
        )
        split_pattern = r"(%s)" % custom_pattern

    seg = ""
    for text, image, block_type in sections:
        normalized_type = block_type if block_type in ("table", "image") else "text"
        text = ("\n" + str(text)) if text else ""

        if normalized_type == "table":
            ck_text = text
            cks.append(
                {
                    "text": ck_text,
                    "image": image,
                    "ck_type": "table",
                    "tk_nums": num_tokens_from_string(ck_text),
                }
            )
            continue
        if normalized_type == "image":
            cks.append(
                {
                    "text": text,
                    "image": image,
                    "ck_type": "image",
                    "tk_nums": num_tokens_from_string(text),
                }
            )
            continue

        if has_custom:
            split_sec = re.split(split_pattern, text)
            for sub_sec in split_sec:
                if not sub_sec or not sub_sec.strip():
                    if seg and seg.strip():
                        s = seg.strip()
                        cks.append(
                            {
                                "text": s,
                                "image": None,
                                "ck_type": "text",
                                "tk_nums": num_tokens_from_string(s),
                            }
                        )
                    seg = ""
                    continue
                if re.fullmatch(custom_pattern, sub_sec.strip()):
                    if seg and seg.strip():
                        s = seg.strip()
                        cks.append(
                            {
                                "text": s,
                                "image": None,
                                "ck_type": "text",
                                "tk_nums": num_tokens_from_string(s),
                            }
                        )
                    seg = ""
                    continue
                seg += sub_sec
        else:
            if text and text.strip():
                t = text.strip()
                cks.append(
                    {
                        "text": t,
                        "image": None,
                        "ck_type": "text",
                        "tk_nums": num_tokens_from_string(t),
                    }
                )

    if has_custom and seg and seg.strip():
        s = seg.strip()
        cks.append(
            {
                "text": s,
                "image": None,
                "ck_type": "text",
                "tk_nums": num_tokens_from_string(s),
            }
        )

    return cks, has_custom


def merge_docx_like_cks(
    cks: list[dict[str, Any]],
    chunk_token_num: int,
    has_custom: bool,
    min_chunk_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Merge chunks following RAGFlow _merge_cks style.

    *min_chunk_tokens*: short text chunks below this threshold are force-merged
    into the previous text chunk even if it already exceeded chunk_token_num.
    """
    merged: list[dict[str, Any]] = []
    prev_text_ck = -1
    for ck in cks:
        ck_type = ck["ck_type"]
        if ck_type != "text":
            merged.append(ck)
            continue

        is_short = min_chunk_tokens > 0 and ck.get("tk_nums", 0) < min_chunk_tokens

        if (
            prev_text_ck < 0
            or (merged[prev_text_ck]["tk_nums"] >= chunk_token_num and not is_short)
            or (has_custom and not is_short)
        ):
            merged.append(ck)
            prev_text_ck = len(merged) - 1
            continue

        merged[prev_text_ck]["text"] = (merged[prev_text_ck].get("text") or "") + (
            ck.get("text") or ""
        )
        merged[prev_text_ck]["tk_nums"] = merged[prev_text_ck].get("tk_nums", 0) + ck.get(
            "tk_nums", 0
        )
    return merged


def add_context_docx_like(cks: list[dict[str, Any]], idx: int, context_size: int) -> None:
    """Add around-text context for table/image chunks (RAGFlow-like)."""
    if cks[idx]["ck_type"] not in ("image", "table") or context_size <= 0:
        return
    prev = idx - 1
    after = idx + 1
    remain_above = context_size
    remain_below = context_size
    cks[idx]["context_above"] = ""
    cks[idx]["context_below"] = ""
    split_pat = r"([。!?？；！\n]|\. )"

    def take_sentences_from_end(cnt: str, need_tokens: int) -> str:
        txts = re.split(split_pat, cnt, flags=re.DOTALL)
        sents: list[str] = []
        for j in range(0, len(txts), 2):
            sents.append(txts[j] + (txts[j + 1] if j + 1 < len(txts) else ""))
        acc = ""
        for s in reversed(sents):
            acc = s + acc
            if num_tokens_from_string(acc) >= need_tokens:
                break
        return acc

    def take_sentences_from_start(cnt: str, need_tokens: int) -> str:
        txts = re.split(split_pat, cnt, flags=re.DOTALL)
        acc = ""
        for j in range(0, len(txts), 2):
            acc += txts[j] + (txts[j + 1] if j + 1 < len(txts) else "")
            if num_tokens_from_string(acc) >= need_tokens:
                break
        return acc

    parts_above: list[str] = []
    while prev >= 0 and remain_above > 0:
        if cks[prev]["ck_type"] == "text":
            tk = cks[prev]["tk_nums"]
            if tk >= remain_above:
                parts_above.insert(0, take_sentences_from_end(cks[prev]["text"], remain_above))
                remain_above = 0
                break
            parts_above.insert(0, cks[prev]["text"])
            remain_above -= tk
        prev -= 1

    parts_below: list[str] = []
    while after < len(cks) and remain_below > 0:
        if cks[after]["ck_type"] == "text":
            tk = cks[after]["tk_nums"]
            if tk >= remain_below:
                parts_below.append(take_sentences_from_start(cks[after]["text"], remain_below))
                remain_below = 0
                break
            parts_below.append(cks[after]["text"])
            remain_below -= tk
        after += 1

    cks[idx]["context_above"] = "".join(parts_above) if parts_above else ""
    cks[idx]["context_below"] = "".join(parts_below) if parts_below else ""


def chunk_semantic_ragflow(
    text_blocks: list[DocumentBlock],
    chunk_size: int,
    chunk_overlap: int,
    chunk_method: str | None,
    *,
    fixed_size_fallback: Callable[[list[DocumentBlock], int, int], list[Chunk]],
    min_chunk_tokens: int = 0,
) -> list[Chunk]:
    """Semantic chunking with RAGFlow-like naive merge and docx-like paths."""
    _ = chunk_method
    delimiter = os.environ.get("OPENRAG_CHUNK_DELIMITER", "\n!?;。；！？")
    overlapped_percent = int(os.environ.get("OPENRAG_CHUNK_OVERLAPPED_PERCENT", "0"))
    if overlapped_percent < 0:
        overlapped_percent = 0
    if overlapped_percent > 100:
        overlapped_percent = 100

    if overlapped_percent == 0 and chunk_overlap > 0 and chunk_size > 0:
        overlapped_percent = min(100, max(0, int((chunk_overlap / chunk_size) * 100)))

    children_delimiter = os.environ.get("OPENRAG_CHILDREN_DELIMITER", "")
    strict_limit = os.environ.get("OPENRAG_STRICT_TOKEN_LIMIT", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    table_context_size = max(0, int(os.environ.get("OPENRAG_TABLE_CONTEXT_SIZE", "0")))
    image_context_size = max(0, int(os.environ.get("OPENRAG_IMAGE_CONTEXT_SIZE", "0")))

    base_sections: list[tuple[str, Optional[bytes], Optional[str]]] = []
    text_sections_for_naive: list[tuple[str, str]] = []
    has_non_text = False
    for block in text_blocks:
        txt = (block.text or "").strip()
        if not txt:
            continue
        bt = (block.block_type or "text").lower()
        if bt not in ("table", "image"):
            bt = "text"
        if bt != "text":
            has_non_text = True
        base_sections.append((txt, block.image, bt))
        text_sections_for_naive.append((txt, ""))

    merged_items: list[dict[str, Any]] = []
    if has_non_text:
        cks, has_custom = build_docx_like_cks(base_sections, delimiter)
        if table_context_size > 0:
            for i, ck in enumerate(cks):
                if ck["ck_type"] == "table":
                    add_context_docx_like(cks, i, table_context_size)
        if image_context_size > 0:
            for i, ck in enumerate(cks):
                if ck["ck_type"] == "image":
                    add_context_docx_like(cks, i, image_context_size)
        merged_items = merge_docx_like_cks(cks, chunk_size, has_custom, min_chunk_tokens)
    else:
        merged_texts = ragflow_naive_merge(
            sections=text_sections_for_naive,
            chunk_token_num=chunk_size,
            delimiter=delimiter,
            overlapped_percent=overlapped_percent,
            strict_limit=strict_limit,
            min_chunk_tokens=min_chunk_tokens,
        )
        merged_items = [
            {
                "text": mt,
                "ck_type": "text",
                "image": None,
            }
            for mt in merged_texts
        ]

    non_empty_merged = [
        (m.get("text") or "").strip() for m in merged_items if (m.get("text") or "").strip()
    ]
    total_text_len = sum(len((b.text or "").strip()) for b in text_blocks)
    if len(non_empty_merged) <= 1 and total_text_len > chunk_size:
        return fixed_size_fallback(text_blocks, chunk_size, chunk_overlap)

    joined_blocks: list[str] = []
    block_spans: list[tuple[int, int, DocumentBlock]] = []
    cursor = 0
    for b in text_blocks:
        t = (b.text or "").strip()
        if not t:
            continue
        joined_blocks.append(t)
        block_spans.append((cursor, cursor + len(t), b))
        cursor += len(t) + 1
    full_text = "\n".join(joined_blocks)

    def owner_for_pos(pos: int) -> DocumentBlock:
        if not block_spans:
            return text_blocks[0]
        for s, e, b in block_spans:
            if s <= pos < e:
                return b
        if pos < block_spans[0][0]:
            return block_spans[0][2]
        return block_spans[-1][2]

    def owner_start_abs(pos: int, owner: DocumentBlock) -> int:
        base = owner.char_start if owner.char_start is not None else owner.offset
        for s, e, b in block_spans:
            if b is owner:
                rel = pos - s
                if rel < 0:
                    rel = 0
                return base + rel
        return base + max(0, pos)

    chunks: list[Chunk] = []
    search_from = 0
    fallback_offset = 0
    generated_idx = 0
    for item in merged_items:
        chunk_text = (item.get("text") or "").strip()
        if not chunk_text:
            continue
        pos = find_chunk_pos_robust(full_text, chunk_text, search_from)
        if pos < 0:
            pos = fallback_offset
        search_from = max(pos, search_from)
        fallback_offset = pos + len(chunk_text)
        chunk_end_pos = pos + len(chunk_text)

        covered: list[DocumentBlock] = []
        for s, e, b in block_spans:
            if _overlap_len(pos, chunk_end_pos, s, e) > 0:
                covered.append(b)

        owner = covered[0] if covered else owner_for_pos(pos)
        if not covered:
            covered = [owner]
        start_abs = owner_start_abs(pos, owner)
        end_abs = start_abs + len(chunk_text)

        metadata: dict[str, Any] = {}
        if item.get("context_above"):
            metadata["context_above"] = item["context_above"]
        if item.get("context_below"):
            metadata["context_below"] = item["context_below"]
        ck_type = str(item.get("ck_type") or "text")
        metadata["doc_type_kwd"] = ck_type
        if ck_type in ("table", "image"):
            metadata["ragflow_chunk_type"] = ck_type
        if item.get("image") is not None:
            metadata["has_image"] = True

        child_split_pattern = ""
        if children_delimiter:
            custom_child = [m.group(1) for m in re.finditer(r"`([^`]+)`", children_delimiter)]
            if custom_child:
                child_split_pattern = "|".join(
                    re.escape(t) for t in sorted(set(custom_child), key=len, reverse=True)
                )
            else:
                child_split_pattern = children_delimiter

        child_parts = (
            split_with_pattern(chunk_text, child_split_pattern)
            if child_split_pattern
            else [chunk_text]
        )

        local_cursor = 0
        for part in child_parts:
            part_text = (part or "").strip()
            if not part_text:
                continue
            rel = chunk_text.find(part_text, local_cursor)
            if rel < 0:
                rel = max(0, local_cursor)
            local_cursor = rel + len(part_text)
            ps = start_abs + rel
            pe = ps + len(part_text)
            part_meta = metadata.copy()
            if child_split_pattern:
                part_meta["mom_with_weight"] = chunk_text
            part_meta.update(ragflow_tokenize_fields(part_text))
            poss: list[tuple[float, float, float, float, float]] = []
            seen_pages: set[int] = set()
            for cb in covered:
                if cb.bbox is None or len(cb.bbox) != 4:
                    continue
                x0, y0, x1, y1 = cb.bbox
                poss.append((max(0, cb.page - 1), x0, x1, y0, y1))
                seen_pages.add(cb.page)
                page_bboxes = (cb.metadata or {}).get("page_bboxes")
                if isinstance(page_bboxes, dict):
                    for pg_str, bb in page_bboxes.items():
                        pg_1b = int(pg_str)
                        if pg_1b in seen_pages:
                            continue
                        seen_pages.add(pg_1b)
                        poss.append((pg_1b - 1, bb[0], bb[2], bb[1], bb[3]))
            if not poss:
                ii = generated_idx
                poss = [(ii, ii, ii, ii, ii)]
            part_meta.update(ragflow_add_positions_fields(poss))

            page_for_chunk = owner.page
            bbox_for_chunk = owner.bbox
            same_page_boxes = [
                cb.bbox for cb in covered if cb.page == page_for_chunk and cb.bbox is not None
            ]
            if same_page_boxes:
                xs0 = [float(bb[0]) for bb in same_page_boxes]
                ys0 = [float(bb[1]) for bb in same_page_boxes]
                xs1 = [float(bb[2]) for bb in same_page_boxes]
                ys1 = [float(bb[3]) for bb in same_page_boxes]
                bbox_for_chunk = (min(xs0), min(ys0), max(xs1), max(ys1))
            chunks.append(
                Chunk(
                    text=part_text,
                    chunk_id=str(uuid.uuid4()),
                    page=page_for_chunk,
                    start_offset=ps,
                    end_offset=pe,
                    bbox=bbox_for_chunk,
                    level=owner.level,
                    block_type=(item.get("ck_type") or owner.block_type),
                    source_block_id=owner.block_id,
                    source_char_start=ps,
                    source_char_end=pe,
                    metadata=part_meta,
                )
            )
            generated_idx += 1

    return chunks


def ragflow_semantic_chunk(
    texts: list[str],
    chunk_token_num: int,
    delimiter: str,
    children_delimiter: str = "",
) -> list[str]:
    """Lightweight helper: naive-merge text sections then optional children split.

    Used by parity tests to assert delimiter / children behavior without full ``DocumentBlock`` flow.
    """
    sections = [(t, "") for t in texts]
    merged = ragflow_naive_merge(
        sections=sections,
        chunk_token_num=chunk_token_num,
        delimiter=delimiter,
        overlapped_percent=0,
        strict_limit=False,
    )
    if not children_delimiter:
        return [m.strip() for m in merged if m.strip()]
    out: list[str] = []
    custom_child = [m.group(1) for m in re.finditer(r"`([^`]+)`", children_delimiter)]
    child_pat = (
        "|".join(re.escape(t) for t in sorted(set(custom_child), key=len, reverse=True))
        if custom_child
        else children_delimiter
    )
    for m in merged:
        m = m.strip()
        if not m:
            continue
        out.extend(p.strip() for p in split_with_pattern(m, child_pat) if p.strip())
    return out
