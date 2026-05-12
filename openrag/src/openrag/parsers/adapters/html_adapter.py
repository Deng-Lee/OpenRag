"""HTML parser adapter."""

from bs4 import BeautifulSoup

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock
from openrag.parsers.char_spans import paragraph_blocks_from_plaintext


class HtmlParserAdapter(RAGFlowParserAdapter):
    """HTML：DOM 抽纯文本后按段落建块；坐标为**抽取文本流**中的字符下标。"""

    supported_extensions = ('.html', '.htm')

    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.html_parser import RAGFlowHtmlParser
        self.ragflow_parser = RAGFlowHtmlParser()

    def parse(self, file_path: str) -> list[DocumentBlock]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        soup = BeautifulSoup(raw, "html.parser")
        plain = soup.get_text("\n\n")
        blocks = paragraph_blocks_from_plaintext(plain, id_prefix="html")
        for b in blocks:
            b.metadata = (b.metadata or {}) | {"text_stream": "html_extracted"}
        return blocks
