"""Base adapter for RAGFlow parsers."""

from abc import ABC
from typing import Tuple

from openrag.parsers.base import DocumentBlock, DocumentParser


class RAGFlowParserAdapter(DocumentParser, ABC):
    """RAGFlow 解析器适配器基类"""

    supported_extensions: Tuple[str, ...] = ()

    def __init__(self):
        self.ragflow_parser = None

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """解析文档并转换为 DocumentBlock"""
        raw_result = self.ragflow_parser(file_path)

        blocks = []
        for idx, item in enumerate(raw_result):
            content = item[0] if isinstance(item, (list, tuple)) else item
            image = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else None

            block = self._convert_to_document_block(content, image, idx)
            blocks.append(block)

        return blocks

    def supports(self, file_path: str) -> bool:
        """检查文件扩展名"""
        return file_path.lower().endswith(self.supported_extensions)

    def _convert_to_document_block(
        self,
        content: str,
        image: bytes,
        index: int
    ) -> DocumentBlock:
        """转换为 DocumentBlock（子类可覆盖）"""
        return DocumentBlock(
            text=content,
            page=self._extract_page(content, index),
            offset=index,
            image=image,
            block_type=self._detect_block_type(content),
            level=self._detect_heading_level(content),
            layout_type=self._detect_layout_type(content, image)
        )

    def _extract_page(self, content: str, index: int) -> int:
        """提取页码（子类可覆盖）"""
        return 1

    def _detect_block_type(self, content: str) -> str:
        """检测块类型（子类可覆盖）"""
        return "text"

    def _detect_heading_level(self, content: str) -> int:
        """检测标题层级（子类可覆盖）"""
        return 0

    def _detect_layout_type(self, content: str, image: bytes) -> str:
        """检测布局类型（子类可覆盖）"""
        if image:
            return "figure"
        return "text"
