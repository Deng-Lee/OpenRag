"""Parser factory with lazy loading."""

import os
import importlib
from typing import Dict, Optional

from .base import DocumentParser
from openrag.chunking.excel_config import ExcelChunkingConfig


class ParserFactory:
    """解析器懒加载工厂"""

    def __init__(self):
        self._parsers: Dict[str, DocumentParser] = {}
        self._parser_classes = {
            '.pdf': 'openrag.parsers.adapters.pdf_adapter.PDFParserAdapter',
            '.docx': 'openrag.parsers.adapters.docx_adapter.DocxParserAdapter',
            '.doc': 'openrag.parsers.adapters.docx_adapter.DocxParserAdapter',
            '.xlsx': 'openrag.parsers.adapters.excel_adapter.ExcelParserAdapter',
            '.xls': 'openrag.parsers.adapters.excel_adapter.ExcelParserAdapter',
            '.csv': 'openrag.parsers.adapters.excel_adapter.ExcelParserAdapter',
            '.pptx': 'openrag.parsers.adapters.ppt_adapter.PptParserAdapter',
            '.ppt': 'openrag.parsers.adapters.ppt_adapter.PptParserAdapter',
            '.html': 'openrag.parsers.adapters.html_adapter.HtmlParserAdapter',
            '.htm': 'openrag.parsers.adapters.html_adapter.HtmlParserAdapter',
            '.md': 'openrag.parsers.adapters.markdown_adapter.MarkdownParserAdapter',
            '.markdown': 'openrag.parsers.adapters.markdown_adapter.MarkdownParserAdapter',
            '.mdx': 'openrag.parsers.adapters.markdown_adapter.MarkdownParserAdapter',
            '.txt': 'openrag.parsers.adapters.txt_adapter.TxtParserAdapter',
            '.py': 'openrag.parsers.adapters.txt_adapter.TxtParserAdapter',
            '.js': 'openrag.parsers.adapters.txt_adapter.TxtParserAdapter',
            '.json': 'openrag.parsers.adapters.json_adapter.JsonParserAdapter',
            '.jsonl': 'openrag.parsers.adapters.json_adapter.JsonParserAdapter',
            '.ldjson': 'openrag.parsers.adapters.json_adapter.JsonParserAdapter',
            '.epub': 'openrag.parsers.adapters.epub_adapter.EpubParserAdapter',
        }
        # 解析器类型映射（用于用户指定类型）
        self._parser_type_map = {
            'pdf': '.pdf',
            'docx': '.docx',
            'doc': '.doc',
            'xlsx': '.xlsx',
            'xls': '.xls',
            'csv': '.csv',
            'pptx': '.pptx',
            'ppt': '.ppt',
            'html': '.html',
            'htm': '.html',
            'md': '.md',
            'markdown': '.md',
            'mdx': '.mdx',
            'txt': '.txt',
            'py': '.py',
            'js': '.js',
            'json': '.json',
            'jsonl': '.jsonl',
            'ldjson': '.ldjson',
            'epub': '.epub',
        }
        # Excel 切片配置
        self._excel_config: Optional[ExcelChunkingConfig] = None

    def set_excel_config(self, config: ExcelChunkingConfig):
        """设置 Excel 切片配置

        Args:
            config: ExcelChunkingConfig 实例
        """
        self._excel_config = config
        # 清除已缓存的 Excel 解析器，以便使用新配置重新创建
        for ext in ['.xlsx', '.xls', '.csv']:
            if ext in self._parsers:
                del self._parsers[ext]

    def get_parser(self, file_path: str, **kwargs) -> DocumentParser:
        """获取解析器（懒加载）

        Args:
            file_path: 文件路径
            **kwargs: 额外参数，可用于传递配置

        Returns:
            DocumentParser 实例

        Raises:
            ValueError: 不支持的文件格式
        """
        ext = os.path.splitext(file_path)[1].lower()

        if ext in self._parsers:
            return self._parsers[ext]

        if ext in self._parser_classes:
            parser = self._load_parser(ext, **kwargs)
            self._parsers[ext] = parser
            return parser

        raise ValueError(f"不支持的文件格式: {ext}")

    def _load_parser(self, ext: str, **kwargs) -> DocumentParser:
        """动态加载解析器类"""
        class_path = self._parser_classes[ext]
        module_path, class_name = class_path.rsplit('.', 1)
        module = importlib.import_module(module_path)
        parser_class = getattr(module, class_name)

        # Excel 解析器支持传入配置
        if ext in ['.xlsx', '.xls', '.csv']:
            config = kwargs.get('excel_config') or self._excel_config
            return parser_class(config=config)

        return parser_class()

    def get_parser_by_type(self, parser_type: str) -> DocumentParser:
        """根据类型获取解析器（用于用户指定解析类型）

        Args:
            parser_type: 解析器类型（如 'pdf', 'docx', 'txt' 等）

        Returns:
            DocumentParser 实例

        Raises:
            ValueError: 不支持的解析器类型
        """
        if parser_type == 'auto':
            raise ValueError("请使用 get_parser() 方法进行自动检测")

        if parser_type not in self._parser_type_map:
            raise ValueError(f"不支持的解析器类型: {parser_type}")

        ext = self._parser_type_map[parser_type]

        if ext in self._parsers:
            return self._parsers[ext]

        parser = self._load_parser(ext)
        self._parsers[ext] = parser
        return parser
