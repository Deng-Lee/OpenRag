"""
Document hierarchy builder - generates L0/L1/L2 for single documents.

Aligned with OpenViking context layers:
  L0 abstract <- summarize L1 overview
  L1 overview <- full document (chunks)
  L2 <- raw chunks (stored as chunks/*.md by HierarchyStorage)

Reference: https://github.com/volcengine/OpenViking (concept docs)
License: AGPL-3.0 (original TreeBuilder adaptation)
"""

from typing import List

from .models import HierarchyResult, Section
from .utils import clean_text, extract_key_sentences, truncate_to_tokens


class DocumentHierarchyBuilder:
    """Builds L0/L1/L2 hierarchy for a single document."""

    def __init__(
        self,
        l0_max_tokens: int = 100,
        l1_max_tokens: int = 2000,
        l1_section_preview_tokens: int = 200,
    ):
        """Initialize builder.

        Args:
            l0_max_tokens: Maximum tokens for L0 abstract (~100 per OpenViking).
            l1_max_tokens: Cap for entire L1 overview markdown (~1k–2k).
            l1_section_preview_tokens: Preview length per heading in Structure section.
        """
        self.l0_max_tokens = l0_max_tokens
        self.l1_max_tokens = l1_max_tokens
        self.l1_section_preview_tokens = l1_section_preview_tokens

    def build_hierarchy(self, chunks: List) -> HierarchyResult:
        """Generate L0/L1/L2 hierarchy from chunks.

        Order: build L1 overview from all chunk text, then derive L0 from that overview.
        """
        if not chunks:
            return HierarchyResult(l0="", l1="", l2=[])

        if self._is_excel_chunks(chunks):
            l1_md = self._build_excel_overview_markdown(chunks)
        else:
            l1_md = self._build_overview_markdown(chunks)
        l1_md = truncate_to_tokens(clean_text(l1_md), self.l1_max_tokens)
        l0 = self._abstract_from_overview(l1_md)

        return HierarchyResult(l0=l0, l1=l1_md, l2=chunks)

    def _abstract_from_overview(self, overview_md: str) -> str:
        """L0: short abstract from L1 overview only (~100 tokens)."""
        text = clean_text(overview_md)
        if not text:
            return ""

        sentences = extract_key_sentences(text, max_sentences=3)
        if sentences:
            candidate = " ".join(sentences)
        else:
            candidate = text

        return truncate_to_tokens(clean_text(candidate), self.l0_max_tokens)

    def _build_overview_markdown(self, chunks: List) -> str:
        """L1: markdown overview + navigation hints pointing at chunks/NNNN.md."""
        full_body = "\n\n".join(
            getattr(c, "text", str(c)) for c in chunks
        )
        summary_budget = min(self.l1_max_tokens // 2, 1500)
        narrative = truncate_to_tokens(clean_text(full_body), summary_budget)

        lines = [
            "# Document overview\n\n",
            narrative,
            "\n\n## Structure\n\n",
        ]

        has_headings = False
        for i, chunk in enumerate(chunks):
            level = getattr(chunk, "level", 0)
            text = getattr(chunk, "text", str(chunk))
            if level <= 0:
                continue
            has_headings = True
            title = text.split("\n")[0].strip().lstrip("#").strip()
            if i + 1 < len(chunks):
                preview_source = getattr(chunks[i + 1], "text", "")
            else:
                preview_source = text
            preview = truncate_to_tokens(
                clean_text(preview_source), self.l1_section_preview_tokens
            )
            lines.append(
                f"- **{title}** (L2: `chunks/{i:04d}.md`): {preview}\n"
            )

        if not has_headings:
            lines.append("\n## Chunks\n\n")
            for i, chunk in enumerate(chunks[:50]):
                t = clean_text(getattr(chunk, "text", str(chunk)))
                prev = truncate_to_tokens(t, 120)
                lines.append(f"- `chunks/{i:04d}.md`: {prev}\n")

        return "".join(lines)

    @staticmethod
    def _is_excel_chunks(chunks: List) -> bool:
        return bool(chunks) and all(
            isinstance(getattr(chunk, "metadata", None), dict)
            and chunk.metadata.get("source_format") == "excel"
            and chunk.metadata.get("sheet_name")
            for chunk in chunks
        )

    def _build_excel_overview_markdown(self, chunks: List) -> str:
        """Build compact Sheet-level navigation for structured Excel chunks."""
        sheets: dict[tuple[int, str], list[tuple[int, object]]] = {}
        for chunk_index, chunk in enumerate(chunks):
            metadata = chunk.metadata
            key = (
                int(metadata.get("sheet_index", 0)),
                str(metadata["sheet_name"]),
            )
            sheets.setdefault(key, []).append((chunk_index, chunk))

        lines = [
            "# Excel workbook overview\n\n",
            f"Sheets: {len(sheets)}; L2 chunks: {len(chunks)}.\n\n",
            "## Sheets\n\n",
        ]
        for (_, sheet_name), entries in sorted(sheets.items()):
            first_chunk_index = entries[0][0]
            last_chunk_index = entries[-1][0]
            metadata_items = [entry[1].metadata for entry in entries]
            row_start = min(item["row_start"] for item in metadata_items)
            row_end = max(item["row_end"] for item in metadata_items)
            column_start = min(item["column_start"] for item in metadata_items)
            column_end = max(item["column_end"] for item in metadata_items)
            lines.append(
                f"- **{sheet_name}** — rows {row_start}-{row_end}; "
                f"columns {column_start}-{column_end}; L2: "
                f"`chunks/{first_chunk_index:04d}.md`–"
                f"`chunks/{last_chunk_index:04d}.md`\n"
            )
        return "".join(lines)

    def _generate_l1_sections(self, chunks: List) -> List[Section]:
        """Build structured sections (used internally / tests); not the on-disk L1 format."""
        sections: List[Section] = []
        for i, chunk in enumerate(chunks):
            level = getattr(chunk, "level", 0)
            text = getattr(chunk, "text", str(chunk))
            if level <= 0:
                continue
            if i + 1 < len(chunks):
                preview_text = getattr(chunks[i + 1], "text", str(chunks[i + 1]))
            else:
                preview_text = text
            preview = truncate_to_tokens(
                clean_text(preview_text), self.l1_section_preview_tokens
            )
            sections.append(
                Section(
                    title=text.split("\n")[0].strip(),
                    level=level,
                    content=preview,
                    start_offset=getattr(chunk, "start_offset", 0),
                    end_offset=getattr(chunk, "end_offset", 0),
                )
            )
        return sections
