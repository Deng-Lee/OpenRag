"""Excel parser adapter with smart chunking strategies."""

import os
from io import BytesIO
from typing import Optional

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock
from openrag.chunking.excel_config import ExcelChunkingConfig


class ExcelParserAdapter(RAGFlowParserAdapter):
    """Excel 解析器适配器 - 支持智能切片策略

    根据表格行数自动选择切片策略：
    - 小表格 (≤ small_table_threshold): 逐行切片
    - 中等表格 (small_table_threshold+1 ~ large_table_threshold): 完整 Markdown
    - 大表格 (> large_table_threshold): HTML 分块，每 chunk_rows 行一块
    """

    supported_extensions = ('.xlsx', '.xls', '.csv')

    def __init__(self, config: Optional[ExcelChunkingConfig] = None):
        """初始化 Excel 解析器适配器

        Args:
            config: Excel 切片配置，如果为 None 使用默认配置
        """
        super().__init__()
        # 直接导入模块，避免触发 ragflow.parser.__init__ 中的其他导入
        import importlib.util
        import sys
        from pathlib import Path

        # 动态导入 excel_parser 模块
        module_path = Path(__file__).parent.parent / "ragflow" / "parser" / "excel_parser.py"
        spec = importlib.util.spec_from_file_location("excel_parser_module", module_path)
        excel_module = importlib.util.module_from_spec(spec)
        sys.modules["excel_parser_module"] = excel_module
        spec.loader.exec_module(excel_module)

        self.ragflow_parser = excel_module.RAGFlowExcelParser()
        self.config = config or ExcelChunkingConfig()

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """解析 Excel 文件，根据行数智能选择切片策略

        Args:
            file_path: Excel 文件路径

        Returns:
            DocumentBlock 列表
        """
        # 读取文件内容
        with open(file_path, 'rb') as f:
            file_content = f.read()

        # 加载 workbook
        file_like = BytesIO(file_content)
        wb = self.ragflow_parser._load_excel_to_workbook(file_like)

        blocks = []
        for sheet_name in wb.sheetnames:
            # 获取当前 Sheet 的行数
            row_count = self.ragflow_parser.get_sheet_row_count(wb, sheet_name)

            if row_count == 0:
                continue

            # 根据行数选择切片策略
            if row_count <= self.config.small_table_threshold:
                # 小表格：逐行切片
                sheet_blocks = self._parse_small_sheet(wb, sheet_name, row_count)
            elif row_count <= self.config.large_table_threshold:
                # 中等表格：完整 Markdown
                sheet_blocks = self._parse_medium_sheet(wb, sheet_name, row_count)
            else:
                # 大表格：HTML 分块
                sheet_blocks = self._parse_large_sheet(wb, sheet_name, row_count)

            blocks.extend(sheet_blocks)

        cursor = 0
        for i, block in enumerate(blocks):
            meta = dict(block.metadata or {})
            sheet = meta.get("sheet_name", "sheet")
            strat = meta.get("strategy", "excel")
            chunk_i = meta.get("chunk_index", i)
            block.block_id = block.block_id or f"xlsx:{sheet}:{strat}:{chunk_i}"
            block.char_start = cursor
            block.char_end = cursor + len(block.text)
            cursor += len(block.text) + 2

        return blocks

    def _parse_small_sheet(self, wb, sheet_name: str, row_count: int) -> list[DocumentBlock]:
        """解析小表格 - 逐行切片

        Args:
            wb: Workbook 对象
            sheet_name: Sheet 名称
            row_count: 行数

        Returns:
            DocumentBlock 列表
        """
        row_chunks = self.ragflow_parser.get_sheet_row_chunks(wb, sheet_name)
        blocks = []

        for idx, line in enumerate(row_chunks):
            metadata = {
                "sheet_name": sheet_name,
                "row_range": f"{idx + 1}",  # 数据行从 1 开始（不含表头）
                "total_rows": row_count,
                "chunk_index": idx,
                "chunk_total": len(row_chunks),
                "strategy": "row_by_row"
            }

            block = DocumentBlock(
                text=line,
                page=1,
                offset=idx,
                block_type="table_row",
                level=0,
                layout_type="table_row",
                metadata=metadata
            )
            blocks.append(block)

        return blocks

    def _parse_medium_sheet(self, wb, sheet_name: str, row_count: int) -> list[DocumentBlock]:
        """解析中等表格 - 完整 Markdown

        Args:
            wb: Workbook 对象
            sheet_name: Sheet 名称
            row_count: 行数

        Returns:
            DocumentBlock 列表（单个元素）
        """
        markdown = self.ragflow_parser.get_sheet_markdown(wb, sheet_name)

        if not markdown:
            return []

        metadata = {
            "sheet_name": sheet_name,
            "row_range": f"1-{row_count}",
            "total_rows": row_count,
            "chunk_index": 0,
            "chunk_total": 1,
            "strategy": "full_markdown"
        }

        block = DocumentBlock(
            text=markdown,
            page=1,
            offset=0,
            block_type="table",
            level=0,
            layout_type="table",
            metadata=metadata
        )

        return [block]

    def _parse_large_sheet(self, wb, sheet_name: str, row_count: int) -> list[DocumentBlock]:
        """解析大表格 - HTML 分块

        Args:
            wb: Workbook 对象
            sheet_name: Sheet 名称
            row_count: 行数

        Returns:
            DocumentBlock 列表
        """
        chunks = self.ragflow_parser.get_sheet_html_chunks(
            wb, sheet_name, self.config.chunk_rows
        )
        blocks = []

        for chunk in chunks:
            metadata = {
                "sheet_name": sheet_name,
                "row_range": chunk["row_range"],
                "total_rows": row_count,
                "chunk_index": chunk["chunk_index"],
                "chunk_total": chunk["chunk_total"],
                "strategy": "html_chunk"
            }

            block = DocumentBlock(
                text=chunk["html"],
                page=1,
                offset=chunk["chunk_index"],
                block_type="table",
                level=0,
                layout_type="table",
                metadata=metadata
            )
            blocks.append(block)

        return blocks
