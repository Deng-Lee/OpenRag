"""Parser registry for managing multiple document parsers."""

from .base import DocumentBlock, DocumentParser
from .factory import ParserFactory


class ParserRegistry:
    """解析器注册表

    使用 ParserFactory 实现懒加载机制
    """

    def __init__(self) -> None:
        """初始化解析器注册表"""
        self.factory = ParserFactory()

    def get_parser(self, file_path: str, parser_type: str = 'auto') -> DocumentParser:
        """获取适合的解析器（懒加载）

        Args:
            file_path: 文件路径（当 parser_type='auto' 时用于检测类型）
            parser_type: 解析器类型，默认为 'auto' 自动检测

        Returns:
            DocumentParser 实例

        Raises:
            ValueError: 不支持的文件格式或解析器类型
        """
        if parser_type == 'auto':
            return self.factory.get_parser(file_path)
        else:
            return self.factory.get_parser_by_type(parser_type)

    def parse(self, file_path: str, parser_type: str = 'auto') -> list[DocumentBlock]:
        """解析文件

        Args:
            file_path: 文件路径
            parser_type: 解析器类型，默认为 'auto' 自动检测

        Returns:
            DocumentBlock 列表

        Raises:
            ValueError: 不支持的文件格式或解析器类型
        """
        parser = self.get_parser(file_path, parser_type)
        return parser.parse(file_path)
