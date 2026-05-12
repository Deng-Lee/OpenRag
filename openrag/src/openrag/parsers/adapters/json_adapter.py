"""JSON parser adapter."""

import json

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock


class JsonParserAdapter(RAGFlowParserAdapter):
    """JSON 解析器适配器"""

    supported_extensions = ('.json', '.jsonl', '.ldjson')

    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.json_parser import RAGFlowJsonParser
        self.ragflow_parser = RAGFlowJsonParser()

    def parse(self, file_path: str) -> list[DocumentBlock]:
        lower = file_path.lower()
        with open(file_path, "rb") as f:
            binary = f.read()

        if lower.endswith(".jsonl") or lower.endswith(".ldjson"):
            return self._parse_json_lines(binary)

        # RAGFlowJsonParser.__call__ expects binary (bytes), not a file path
        sections = self.ragflow_parser(binary)

        blocks: list[DocumentBlock] = []
        for idx, section in enumerate(sections or []):
            text = str(section) if not isinstance(section, str) else section
            if not text.strip():
                continue
            blocks.append(DocumentBlock(
                text=text,
                page=1,
                offset=idx,
                block_type="text",
                level=0,
                layout_type="text",
            ))
        return blocks

    def _parse_json_lines(self, binary: bytes) -> list[DocumentBlock]:
        """Line-delimited JSON: one JSON value per non-empty line (RAGFlow-style ingestion)."""
        text = binary.decode("utf-8", errors="ignore")
        blocks: list[DocumentBlock] = []
        idx = 0
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = raw
            line_text = (
                json.dumps(obj, ensure_ascii=False)
                if not isinstance(obj, str)
                else obj
            )
            if not line_text.strip():
                continue
            blocks.append(
                DocumentBlock(
                    text=line_text,
                    page=1,
                    offset=idx,
                    block_type="text",
                    level=0,
                    layout_type="text",
                )
            )
            idx += 1
        return blocks
