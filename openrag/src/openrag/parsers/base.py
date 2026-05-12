"""Base classes for document parsers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class DocumentBlock:
    """文档内容块，包含文本、图片、表格等多种内容类型

    Attributes:
        text: 文本内容
        page: 页码（1-indexed）
        offset: 字符偏移量
        bbox: PDF 坐标 (x1, y1, x2, y2)
        block_type: 块类型（text, table, image, code）
        level: 标题层级（0=正文，1-6=标题）
        image: 图片数据（如果有）
        table_data: 表格结构化数据
        layout_type: 布局类型（title/text/table/figure/list）
        confidence: OCR 置信度（0-1）
        language: 检测到的语言
        metadata: 其他元数据
    """
    text: str
    page: int
    offset: int
    bbox: Optional[tuple[float, float, float, float]] = None
    block_type: str = "text"
    level: int = 0
    #: 解析器内稳定逻辑 id（如 docx:para:3、pdf:page:1:block:0）
    block_id: Optional[str] = None
    #: 整文件「规范化字符流」中的起始下标（含）；与 end 构成半开区间
    char_start: Optional[int] = None
    char_end: Optional[int] = None

    # RAGFlow 增强字段
    image: Optional[bytes] = None
    table_data: Optional[dict] = None
    layout_type: Optional[str] = None
    confidence: Optional[float] = None
    language: Optional[str] = None
    metadata: Optional[dict] = None


class DocumentParser(ABC):
    """文档解析器基类

    所有文档解析器都应该继承此类并实现 parse() 和 supports() 方法
    """

    @abstractmethod
    def parse(self, file_path: str) -> list[DocumentBlock]:
        """解析文档，返回文档块列表

        Args:
            file_path: 文档文件路径

        Returns:
            DocumentBlock 对象列表

        Raises:
            NotImplementedError: 如果解析器尚未实现
            ValueError: 如果文件无法解析
        """
        pass

    @abstractmethod
    def supports(self, file_path: str) -> bool:
        """检查是否支持该文件类型

        Args:
            file_path: 文档文件路径

        Returns:
            如果支持该文件类型返回 True，否则返回 False
        """
        pass
