"""Infer conservative PDF heading levels from structured blocks and outlines."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any, Iterable

from openrag.parsers.base import DocumentBlock


_CN_NUMBER = "〇零一二三四五六七八九十百千万两0-9"
_EXPLICIT_CN_RE = re.compile(
    rf"^第[{_CN_NUMBER}]+\s*(?P<kind>篇|章|节|条)(?:[、.．:\s]|$)"
)
_EXPLICIT_EN_RE = re.compile(
    r"^(?P<kind>part|chapter|section|article|appendix)\s+"
    r"(?:[0-9ivxlcdm]+|[a-z])(?:[.、:\s]|$)",
    re.IGNORECASE,
)
_DOTTED_RE = re.compile(
    r"^(?P<number>\d+(?:[.．]\d+){1,5})(?:[.．、:\s]|$)"
)
_SINGLE_ARABIC_RE = re.compile(r"^(?P<number>\d+)[.．、]\s*")
_CN_LIST_RE = re.compile(rf"^[{_CN_NUMBER}]+[、.．]\s*")
_PAGE_NUMBER_RE = re.compile(r"^[\s\-—–·•]*\d{1,4}[\s\-—–·•]*$")
_TOC_ENTRY_RE = re.compile(r"(?:\.{2,}|…{2,}|·{2,})\s*\d+\s*$")


@dataclass(frozen=True)
class PdfOutlineEntry:
    text: str
    level: int
    page: int | None = None


@dataclass(frozen=True)
class _Numbering:
    family: str
    level: int
    value: tuple[int, ...] | None = None
    explicit_h1: bool = False


def normalize_pdf_outlines(raw: Iterable[Any] | None) -> list[PdfOutlineEntry]:
    entries: list[PdfOutlineEntry] = []
    for item in raw or []:
        if isinstance(item, PdfOutlineEntry):
            entries.append(item)
            continue
        if isinstance(item, dict):
            text = item.get("text") or item.get("title") or item.get("/Title")
            depth = item.get("depth", item.get("level", 0))
            page = item.get("page") or item.get("page_number")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            text = item[0]
            depth = item[1]
            page = item[2] if len(item) >= 3 else None
        else:
            continue
        if not str(text or "").strip():
            continue
        try:
            level = max(1, int(depth) + 1)
        except (TypeError, ValueError):
            level = 1
        try:
            normalized_page = int(page) if page is not None else None
        except (TypeError, ValueError):
            normalized_page = None
        entries.append(PdfOutlineEntry(str(text).strip(), level, normalized_page))
    return entries


def extract_pdf_outlines(file_path: str) -> list[PdfOutlineEntry]:
    """Read PDF outline entries without depending on a parser backend."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        raw_outline = reader.outline
    except Exception:
        return []

    entries: list[PdfOutlineEntry] = []

    def walk(items: Iterable[Any], depth: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            title = getattr(item, "title", None)
            if title is None and isinstance(item, dict):
                title = item.get("/Title") or item.get("title")
            if not str(title or "").strip():
                continue
            try:
                page_index = reader.get_destination_page_number(item)
                page = page_index + 1 if page_index is not None and page_index >= 0 else None
            except Exception:
                page = None
            entries.append(
                PdfOutlineEntry(
                    text=str(title).strip(),
                    level=max(1, depth + 1),
                    page=page,
                )
            )

    try:
        walk(raw_outline or [], 0)
    except Exception:
        return entries
    return entries


class PdfHeadingHierarchyResolver:
    """Resolve heading hierarchy behind one testable interface.

    The resolver mutates the supplied blocks' semantic fields and metadata, then
    returns the same ordered list. Rejected candidates remain ordinary text.
    """

    def resolve(
        self,
        blocks: list[DocumentBlock],
        outlines: Iterable[Any] | None,
    ) -> list[DocumentBlock]:
        if not blocks:
            return blocks

        outline_entries = normalize_pdf_outlines(outlines)
        candidates = self._candidate_indices(blocks, outline_entries)
        outline_matches = self._match_outlines(blocks, candidates, outline_entries)
        numbering = {
            index: self._parse_numbering(blocks[index].text, blocks)
            for index in candidates
        }

        for index, block in enumerate(blocks):
            block.metadata = dict(block.metadata or {})
            block.metadata.setdefault("hard_boundary", False)
            if index not in candidates:
                continue

            block.metadata["heading_candidate"] = True
            if self._is_explicit_document_title(block):
                self._mark_document_title(block)
                continue

            if index in outline_matches:
                entry, score = outline_matches[index]
                sources = self._sources_with_markdown(block, entry.level, "outline")
                self._confirm_heading(
                    block,
                    level=entry.level,
                    source=sources,
                    confidence="hard",
                    score=score,
                )
                continue

            token = numbering[index]
            if token is not None:
                confidence = "hard" if token.explicit_h1 else "supported"
                sources = self._sources_with_markdown(block, token.level, "numbering")
                self._confirm_heading(
                    block,
                    level=token.level,
                    source=sources,
                    confidence=confidence,
                    score=0.92 if token.explicit_h1 else 0.82,
                )
                continue

            markdown_level = _paddle_markdown_level(block)
            if markdown_level is not None:
                style_supported = self._consistent_markdown_geometry(
                    block,
                    blocks,
                    candidates,
                    markdown_level,
                )
                sources = ["paddle_markdown"]
                if style_supported:
                    sources.append("geometry_style")
                self._confirm_heading(
                    block,
                    level=markdown_level,
                    source=sources,
                    confidence="supported" if style_supported else "soft",
                    score=max(_layout_score(block), 0.75 if style_supported else 0.65),
                )
                continue

            block.block_type = "text"
            block.level = 0
            block.metadata["heading_confidence"] = "soft"
            block.metadata["heading_sources"] = ["layout"]
            block.metadata["heading_score"] = _layout_score(block)
            block.metadata["heading_role"] = (
                "document_title" if self._looks_like_document_title(index, block) else "section"
            )
            block.metadata["hard_boundary"] = False

        self._apply_style_anchors(blocks, candidates)
        self._assign_heading_paths(blocks)
        return blocks

    @staticmethod
    def _candidate_indices(
        blocks: list[DocumentBlock],
        outlines: list[PdfOutlineEntry],
    ) -> set[int]:
        outline_texts_by_page: dict[int | None, set[str]] = {}
        for entry in outlines:
            outline_texts_by_page.setdefault(entry.page, set()).add(
                _normalize_text(entry.text)
            )

        candidates: set[int] = set()
        for index, block in enumerate(blocks):
            text = (block.text or "").strip()
            layout_type = (block.layout_type or "").lower()
            block_type = (block.block_type or "text").lower()
            metadata = block.metadata or {}
            ocr_label = str(metadata.get("ocr_label") or "").lower()
            if not text or block_type in {"table", "image", "figure"}:
                continue
            if layout_type in {
                "table",
                "figure",
                "table caption",
                "figure caption",
                "reference",
                "header",
                "footer",
            }:
                continue
            if _PAGE_NUMBER_RE.fullmatch(text) or _TOC_ENTRY_RE.search(text):
                continue
            if len(text) > 180:
                continue

            normalized = _normalize_text(text)
            page_outline_texts = outline_texts_by_page.get(block.page, set())
            unknown_page_texts = outline_texts_by_page.get(None, set())
            outline_candidate = normalized in page_outline_texts or normalized in unknown_page_texts
            if (
                layout_type == "title"
                or block_type in {"title", "heading"}
                or bool(metadata.get("heading_candidate"))
                or ocr_label in {"doc_title", "paragraph_title", "title"}
                or _has_strong_numbering(text)
                or outline_candidate
            ):
                candidates.add(index)
        return candidates

    @staticmethod
    def _match_outlines(
        blocks: list[DocumentBlock],
        candidates: set[int],
        outlines: list[PdfOutlineEntry],
    ) -> dict[int, tuple[PdfOutlineEntry, float]]:
        matches: dict[int, tuple[PdfOutlineEntry, float]] = {}
        used: set[int] = set()
        for entry in outlines:
            best_index: int | None = None
            best_score = 0.0
            for index in candidates:
                if index in used:
                    continue
                block = blocks[index]
                if entry.page is not None and block.page != entry.page:
                    continue
                score = _title_similarity(entry.text, block.text)
                if score > best_score:
                    best_index = index
                    best_score = score
            threshold = 0.80 if entry.page is not None else 0.90
            if best_index is not None and best_score >= threshold:
                matches[best_index] = (entry, best_score)
                used.add(best_index)
        return matches

    @staticmethod
    def _parse_numbering(text: str, blocks: list[DocumentBlock]) -> _Numbering | None:
        stripped = text.strip()
        cn_match = _EXPLICIT_CN_RE.match(stripped)
        if cn_match:
            kind = cn_match.group("kind")
            all_text = "\n".join(block.text for block in blocks if block.text)
            has_part = bool(re.search(rf"第[{_CN_NUMBER}]+\s*篇", all_text))
            has_chapter = bool(re.search(rf"第[{_CN_NUMBER}]+\s*章", all_text))
            if kind == "篇":
                return _Numbering("cn_part", 1, explicit_h1=True)
            if kind == "章":
                return _Numbering("cn_chapter", 2 if has_part else 1, explicit_h1=not has_part)
            if kind == "节":
                return _Numbering("cn_section", 3 if has_part and has_chapter else 2)
            return _Numbering("cn_article", 3 if has_chapter else 2)

        en_match = _EXPLICIT_EN_RE.match(stripped)
        if en_match:
            kind = en_match.group("kind").lower()
            all_text = "\n".join(block.text for block in blocks if block.text).lower()
            has_part = bool(re.search(r"^part\s+", all_text, re.MULTILINE))
            if kind == "part":
                return _Numbering("en_part", 1, explicit_h1=True)
            if kind == "chapter":
                return _Numbering("en_chapter", 2 if has_part else 1, explicit_h1=not has_part)
            if kind in {"section", "article"}:
                return _Numbering(f"en_{kind}", 3 if has_part else 2)
            return _Numbering("en_appendix", 1, explicit_h1=True)

        dotted = _DOTTED_RE.match(stripped)
        if dotted:
            values = tuple(
                int(part) for part in re.split(r"[.．]", dotted.group("number"))
            )
            return _Numbering("arabic_dotted", min(6, len(values)), values)

        single = _SINGLE_ARABIC_RE.match(stripped)
        if single and _consistent_single_numbered_titles(blocks):
            return _Numbering(
                "arabic_single",
                1,
                (int(single.group("number")),),
                explicit_h1=True,
            )
        if _CN_LIST_RE.match(stripped):
            return _Numbering("cn_list", 2)
        return None

    @staticmethod
    def _confirm_heading(
        block: DocumentBlock,
        *,
        level: int,
        source: str | Iterable[str],
        confidence: str,
        score: float,
    ) -> None:
        block.block_type = "heading"
        block.level = max(1, min(6, level))
        block.metadata["heading_candidate"] = True
        block.metadata["heading_role"] = "section"
        block.metadata["heading_confidence"] = confidence
        block.metadata["heading_sources"] = (
            [source] if isinstance(source, str) else list(source)
        )
        block.metadata["heading_score"] = round(float(score), 4)
        block.metadata["hard_boundary"] = (
            block.level == 1 and confidence in {"hard", "supported"}
        )

    @staticmethod
    def _looks_like_document_title(index: int, block: DocumentBlock) -> bool:
        return PdfHeadingHierarchyResolver._is_explicit_document_title(block) or (
            index == 0 and block.page == 1
        )

    @staticmethod
    def _is_explicit_document_title(block: DocumentBlock) -> bool:
        metadata = block.metadata or {}
        return metadata.get("heading_role") == "document_title" or str(
            metadata.get("ocr_label") or ""
        ).lower() == "doc_title"

    @staticmethod
    def _mark_document_title(block: DocumentBlock) -> None:
        block.block_type = "text"
        block.level = 0
        block.metadata["heading_candidate"] = True
        block.metadata["heading_role"] = "document_title"
        block.metadata["heading_confidence"] = "soft"
        block.metadata["heading_sources"] = (
            ["paddle_markdown"]
            if _paddle_markdown_level(block) is not None
            else ["layout"]
        )
        block.metadata["heading_score"] = _layout_score(block)
        block.metadata["hard_boundary"] = False

    @staticmethod
    def _sources_with_markdown(
        block: DocumentBlock,
        level: int,
        primary: str,
    ) -> list[str]:
        sources = [primary]
        markdown_level = _paddle_markdown_level(block)
        if markdown_level is None:
            return sources
        if markdown_level == level:
            sources.append("paddle_markdown")
        else:
            block.metadata["heading_conflicts"] = [
                {
                    "source": "paddle_markdown",
                    "level": markdown_level,
                    "selected_level": level,
                }
            ]
        return sources

    @staticmethod
    def _consistent_markdown_geometry(
        block: DocumentBlock,
        blocks: list[DocumentBlock],
        candidates: set[int],
        level: int,
    ) -> bool:
        value = _style_value(block)
        if value is None:
            return False
        compatible = 0
        for index in candidates:
            other = blocks[index]
            if _paddle_markdown_level(other) != level:
                continue
            other_value = _style_value(other)
            if other_value is None or not _same_style_source(block, other):
                continue
            difference = abs(value - other_value) / max(value, other_value)
            if difference <= 0.08:
                compatible += 1
        return compatible >= 2

    @classmethod
    def _apply_style_anchors(
        cls,
        blocks: list[DocumentBlock],
        candidates: set[int],
    ) -> None:
        anchors = [
            block
            for block in blocks
            if block.block_type == "heading"
            and block.level > 0
            and _style_value(block) is not None
        ]
        for index in candidates:
            block = blocks[index]
            metadata = block.metadata or {}
            if block.level > 0 or metadata.get("heading_role") == "document_title":
                continue
            value = _style_value(block)
            if value is None:
                continue
            compatible: list[tuple[float, DocumentBlock]] = []
            for anchor in anchors:
                anchor_value = _style_value(anchor)
                if anchor_value is None:
                    continue
                if not _same_style_source(block, anchor):
                    continue
                relative_difference = abs(value - anchor_value) / max(value, anchor_value)
                if relative_difference <= 0.08:
                    compatible.append((relative_difference, anchor))
            if not compatible:
                continue
            _, anchor = min(compatible, key=lambda item: item[0])
            cls._confirm_heading(
                block,
                level=anchor.level,
                source="anchored_style",
                confidence="soft",
                score=0.70,
            )

    @staticmethod
    def _assign_heading_paths(blocks: list[DocumentBlock]) -> None:
        stack: list[DocumentBlock] = []
        for block in blocks:
            block.metadata = dict(block.metadata or {})
            if block.block_type == "heading" and block.level > 0:
                while stack and stack[-1].level >= block.level:
                    stack.pop()
                block.metadata["parent_heading_id"] = (
                    stack[-1].block_id if stack else None
                )
                block.metadata["heading_path"] = [item.text for item in stack] + [
                    block.text
                ]
                stack.append(block)
            else:
                block.metadata["active_heading_ids"] = [
                    item.block_id for item in stack if item.block_id
                ]
                block.metadata["heading_path"] = [item.text for item in stack]


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _strip_numbering(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    for pattern in (
        _EXPLICIT_CN_RE,
        _EXPLICIT_EN_RE,
        _DOTTED_RE,
        _SINGLE_ARABIC_RE,
        _CN_LIST_RE,
    ):
        value = pattern.sub("", value, count=1)
    return _normalize_text(value)


def _title_similarity(left: str, right: str) -> float:
    left_normalized = _normalize_text(left)
    right_normalized = _normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    direct = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    left_title = _strip_numbering(left)
    right_title = _strip_numbering(right)
    stripped = (
        SequenceMatcher(None, left_title, right_title).ratio()
        if left_title and right_title
        else 0.0
    )
    return max(direct, stripped)


def _has_strong_numbering(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(
        _EXPLICIT_CN_RE.match(stripped)
        or _EXPLICIT_EN_RE.match(stripped)
        or _DOTTED_RE.match(stripped)
    )


def _consistent_single_numbered_titles(blocks: list[DocumentBlock]) -> bool:
    values: list[int] = []
    for block in blocks:
        if (block.layout_type or "").lower() != "title":
            continue
        match = _SINGLE_ARABIC_RE.match((block.text or "").strip())
        if match:
            values.append(int(match.group("number")))
    if len(values) < 2:
        return False
    return all(current > previous for previous, current in zip(values, values[1:]))


def _style_value(block: DocumentBlock) -> float | None:
    metadata = block.metadata or {}
    for key in ("font_size_median", "char_height_median", "geometry_height_ratio"):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _same_style_source(left: DocumentBlock, right: DocumentBlock) -> bool:
    left_metadata = left.metadata or {}
    right_metadata = right.metadata or {}
    left_source = left_metadata.get("style_source")
    right_source = right_metadata.get("style_source")
    if left_source and right_source and left_source != right_source:
        return False
    left_font = left_metadata.get("font_name_mode")
    right_font = right_metadata.get("font_name_mode")
    return not (left_font and right_font and left_font != right_font)


def _paddle_markdown_level(block: DocumentBlock) -> int | None:
    value = (block.metadata or {}).get("paddle_markdown_level")
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    return level if 1 <= level <= 6 else None


def _layout_score(block: DocumentBlock) -> float:
    metadata = block.metadata or {}
    value = metadata.get("layout_score", metadata.get("deepdoc_layout_score", 0.0))
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
