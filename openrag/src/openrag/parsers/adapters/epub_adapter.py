"""EPUB parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class EpubParserAdapter(RAGFlowParserAdapter):
    """EPUB 解析器适配器"""

    supported_extensions = ('.epub',)

    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.epub_parser import RAGFlowEpubParser
        self.ragflow_parser = RAGFlowEpubParser()
