"""Markdown parser adapter."""

import re

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock


class MarkdownParserAdapter(RAGFlowParserAdapter):
    """Markdown 解析器适配器"""

    supported_extensions = ('.md', '.markdown', '.mdx')

    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.markdown_parser import RAGFlowMarkdownParser
        self.ragflow_parser = RAGFlowMarkdownParser()

    def parse(self, file_path: str) -> list[DocumentBlock]:
        # RAGFlowMarkdownParser has no __call__; use extract_tables_and_remainder
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            md_text = f.read()

        remainder, tables = self.ragflow_parser.extract_tables_and_remainder(md_text)

        blocks: list[DocumentBlock] = []
        idx = 0
        search_from = 0

        for para in re.split(r"\n{2,}", remainder):
            text = para.strip()
            if not text:
                continue
            pos = md_text.find(text, search_from)
            if pos < 0:
                pos = search_from
            c_end = pos + len(text)
            level = self._heading_level(text)
            blocks.append(DocumentBlock(
                text=text,
                page=1,
                offset=idx,
                block_type="heading" if level > 0 else "text",
                level=level,
                layout_type="title" if level > 0 else "text",
                block_id=f"md:para:{idx}",
                char_start=pos,
                char_end=c_end,
            ))
            search_from = c_end
            idx += 1

        for ti, tbl in enumerate(tables):
            raw_tbl = tbl.strip()
            if not raw_tbl:
                continue
            pos = md_text.find(raw_tbl, search_from)
            if pos < 0:
                blocks.append(DocumentBlock(
                    text=raw_tbl,
                    page=1,
                    offset=idx,
                    block_type="table",
                    level=0,
                    layout_type="table",
                    block_id=f"md:table:{ti}",
                ))
            else:
                c_end = pos + len(raw_tbl)
                blocks.append(DocumentBlock(
                    text=raw_tbl,
                    page=1,
                    offset=idx,
                    block_type="table",
                    level=0,
                    layout_type="table",
                    block_id=f"md:table:{ti}",
                    char_start=pos,
                    char_end=c_end,
                ))
                search_from = c_end
            idx += 1

        return blocks

    @staticmethod
    def _heading_level(text: str) -> int:
        m = re.match(r"^(#{1,6})\s", text)
        return len(m.group(1)) if m else 0
