"""Excel parser adapter with smart chunking strategies."""

import os
from datetime import date, datetime, time
from io import BytesIO
from typing import Optional

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock
from openrag.chunking.excel_config import ExcelChunkingConfig


def _create_ragflow_excel_parser():
    """Load the vendored Excel parser without importing the full RAGFlow package."""
    import importlib.util
    import sys
    from pathlib import Path

    module_path = Path(__file__).parent.parent / "ragflow" / "parser" / "excel_parser.py"
    spec = importlib.util.spec_from_file_location("excel_parser_module", module_path)
    excel_module = importlib.util.module_from_spec(spec)
    sys.modules["excel_parser_module"] = excel_module
    spec.loader.exec_module(excel_module)
    return excel_module.RAGFlowExcelParser()


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
        self.ragflow_parser = _create_ragflow_excel_parser()
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


class StructuredExcelParserAdapter(RAGFlowParserAdapter):
    """Parse Excel workbooks into stable row/column blocks for token chunking."""

    supported_extensions = ('.xlsx', '.xls')
    parser_version = "excel-structured-v2"

    def __init__(self):
        super().__init__()
        self.ragflow_parser = _create_ragflow_excel_parser()

    @staticmethod
    def _is_empty(value) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    @staticmethod
    def _normalize_value(value) -> Optional[str]:
        if StructuredExcelParserAdapter._is_empty(value):
            return None
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return str(value)

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """Return one structured ``DocumentBlock`` for every non-empty sheet."""
        with open(file_path, 'rb') as source:
            workbook = self.ragflow_parser._load_excel_to_workbook(
                BytesIO(source.read())
            )

        blocks: list[DocumentBlock] = []
        cursor = 0
        for sheet_index, sheet_name in enumerate(workbook.sheetnames):
            worksheet = workbook[sheet_name]
            rows = self.ragflow_parser._get_rows_limited(worksheet)
            populated_rows = [
                (row_index, row)
                for row_index, row in enumerate(rows, 1)
                if any(not self._is_empty(cell.value) for cell in row)
            ]
            if not populated_rows:
                continue

            last_column = max(
                column_index
                for _, row in populated_rows
                for column_index, cell in enumerate(row, 1)
                if not self._is_empty(cell.value)
            )
            header_row_index, header_row = populated_rows[0]
            header = []
            for column_index in range(1, last_column + 1):
                value = self._normalize_value(header_row[column_index - 1].value)
                header.append(
                    {
                        "column_index": column_index,
                        "name": value or f"Column_{column_index}",
                    }
                )

            data_rows = []
            for row_index, row in populated_rows[1:]:
                values = [
                    self._normalize_value(row[column_index - 1].value)
                    for column_index in range(1, last_column + 1)
                ]
                if any(value is not None for value in values):
                    data_rows.append({"row_index": row_index, "values": values})
            preview_lines = [
                f"Sheet: {sheet_name}",
                " | ".join(item["name"] for item in header),
            ]
            preview_lines.extend(
                " | ".join(value or "" for value in row["values"])
                for row in data_rows
            )
            text = "\n".join(preview_lines)
            blocks.append(
                DocumentBlock(
                    text=text,
                    page=sheet_index + 1,
                    offset=cursor,
                    block_type="table",
                    level=0,
                    block_id=f"xlsx:{sheet_index}:table",
                    char_start=cursor,
                    char_end=cursor + len(text),
                    table_data={
                        "schema_version": "excel-table-v1",
                        "sheet_name": sheet_name,
                        "sheet_index": sheet_index,
                        "header_row_index": header_row_index,
                        "header": header,
                        "rows": data_rows,
                    },
                    layout_type="table",
                    metadata={
                        "source_format": "excel",
                        "sheet_name": sheet_name,
                        "sheet_index": sheet_index,
                        "row_start": (
                            data_rows[0]["row_index"]
                            if data_rows else header_row_index
                        ),
                        "row_end": (
                            data_rows[-1]["row_index"]
                            if data_rows else header_row_index
                        ),
                        "total_rows": len(data_rows),
                        "strategy": "excel_table_token_v1",
                    },
                )
            )
            cursor += len(text) + 2

        return blocks
