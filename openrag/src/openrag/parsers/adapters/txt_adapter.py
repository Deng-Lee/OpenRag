"""Text parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock
from openrag.parsers.char_spans import paragraph_blocks_from_plaintext


class TxtParserAdapter(RAGFlowParserAdapter):
    """纯文本：按段落切分 + 全文件字符流偏移（utf-8 解码后的字符串下标）。"""

    supported_extensions = ('.txt', '.py', '.js')

    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.txt_parser import RAGFlowTxtParser
        self.ragflow_parser = RAGFlowTxtParser()

    def parse(self, file_path: str) -> list[DocumentBlock]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        return paragraph_blocks_from_plaintext(raw, id_prefix="txt")
