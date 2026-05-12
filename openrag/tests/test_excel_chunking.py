"""Tests for Excel smart chunking functionality."""

import pytest
import pandas as pd
from io import BytesIO
from unittest.mock import Mock, patch, MagicMock

from openrag.chunking.excel_config import ExcelChunkingConfig
from openrag.parsers.adapters.excel_adapter import ExcelParserAdapter
from openrag.parsers.base import DocumentBlock


class TestExcelChunkingConfig:
    """Test ExcelChunkingConfig"""

    def test_default_values(self):
        """测试默认值"""
        config = ExcelChunkingConfig()
        assert config.small_table_threshold == 10
        assert config.large_table_threshold == 200
        assert config.chunk_rows == 256

    def test_custom_values(self):
        """测试自定义值"""
        config = ExcelChunkingConfig(
            small_table_threshold=5,
            large_table_threshold=100,
            chunk_rows=128
        )
        assert config.small_table_threshold == 5
        assert config.large_table_threshold == 100
        assert config.chunk_rows == 128

    def test_invalid_small_threshold(self):
        """测试无效的小表格阈值"""
        with pytest.raises(ValueError, match="small_table_threshold must be >= 1"):
            ExcelChunkingConfig(small_table_threshold=0)

    def test_invalid_threshold_order(self):
        """测试阈值顺序错误"""
        with pytest.raises(ValueError, match="large_table_threshold .* must be >= .*small_table_threshold"):
            ExcelChunkingConfig(small_table_threshold=100, large_table_threshold=50)

    def test_invalid_chunk_rows(self):
        """测试无效的分块行数"""
        with pytest.raises(ValueError, match="chunk_rows must be >= 1"):
            ExcelChunkingConfig(chunk_rows=0)


class TestExcelParserAdapter:
    """Test ExcelParserAdapter with smart chunking"""

    @pytest.fixture
    def adapter(self):
        """创建默认适配器"""
        return ExcelParserAdapter()

    @pytest.fixture
    def custom_adapter(self):
        """创建自定义配置适配器"""
        config = ExcelChunkingConfig(
            small_table_threshold=5,
            large_table_threshold=15,
            chunk_rows=10
        )
        return ExcelParserAdapter(config=config)

    def create_mock_workbook(self, sheet_configs):
        """创建模拟 Workbook

        Args:
            sheet_configs: dict of {sheet_name: row_count}
        """
        mock_wb = MagicMock()
        mock_wb.sheetnames = list(sheet_configs.keys())

        # Mock get_sheet_row_count
        def mock_row_count(wb, sheet_name):
            return sheet_configs.get(sheet_name, 0)

        return mock_wb, sheet_configs

    @patch('builtins.open')
    def test_small_table_row_by_row(self, mock_open):
        """测试小表格逐行切片"""
        # Setup mock parser
        mock_parser = MagicMock()

        mock_wb = MagicMock()
        mock_wb.sheetnames = ['Sheet1']
        mock_parser._load_excel_to_workbook.return_value = mock_wb
        mock_parser.get_sheet_row_count.return_value = 5
        mock_parser.get_sheet_row_chunks.return_value = [
            "Name: Alice; Age: 25",
            "Name: Bob; Age: 30",
            "Name: Charlie; Age: 35",
        ]

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        # Execute
        blocks = adapter.parse('test.xlsx')

        # Verify
        assert len(blocks) == 3
        for i, block in enumerate(blocks):
            assert isinstance(block, DocumentBlock)
            assert block.block_type == "table_row"
            assert block.metadata["sheet_name"] == "Sheet1"
            assert block.metadata["strategy"] == "row_by_row"
            assert block.metadata["chunk_index"] == i
            assert block.metadata["chunk_total"] == 3

    @patch('builtins.open')
    def test_medium_table_markdown(self, mock_open):
        """测试中等表格 Markdown 切片"""
        # Setup mock parser
        mock_parser = MagicMock()

        mock_wb = MagicMock()
        mock_wb.sheetnames = ['Sheet1']
        mock_parser._load_excel_to_workbook.return_value = mock_wb
        mock_parser.get_sheet_row_count.return_value = 50
        mock_parser.get_sheet_markdown.return_value = "| Name | Age |\n|------|-----|\n| Alice | 25 |"

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        # Execute
        blocks = adapter.parse('test.xlsx')

        # Verify
        assert len(blocks) == 1
        block = blocks[0]
        assert isinstance(block, DocumentBlock)
        assert block.block_type == "table"
        assert block.metadata["sheet_name"] == "Sheet1"
        assert block.metadata["strategy"] == "full_markdown"
        assert block.metadata["chunk_total"] == 1
        assert "| Name | Age |" in block.text

    @patch('builtins.open')
    def test_large_table_html_chunks(self, mock_open):
        """测试大表格 HTML 分块"""
        # Setup mock parser
        mock_parser = MagicMock()

        mock_wb = MagicMock()
        mock_wb.sheetnames = ['Sheet1']
        mock_parser._load_excel_to_workbook.return_value = mock_wb
        mock_parser.get_sheet_row_count.return_value = 300
        mock_parser.get_sheet_html_chunks.return_value = [
            {"html": "<table>chunk1</table>", "row_range": "1-128", "chunk_index": 0, "chunk_total": 3},
            {"html": "<table>chunk2</table>", "row_range": "129-256", "chunk_index": 1, "chunk_total": 3},
            {"html": "<table>chunk3</table>", "row_range": "257-300", "chunk_index": 2, "chunk_total": 3},
        ]

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        # Execute
        blocks = adapter.parse('test.xlsx')

        # Verify
        assert len(blocks) == 3
        for i, block in enumerate(blocks):
            assert isinstance(block, DocumentBlock)
            assert block.block_type == "table"
            assert block.metadata["sheet_name"] == "Sheet1"
            assert block.metadata["strategy"] == "html_chunk"
            assert block.metadata["chunk_index"] == i
            assert block.metadata["chunk_total"] == 3
            assert f"<table>chunk{i+1}</table>" == block.text

    @patch('builtins.open')
    def test_boundary_small_to_medium(self, mock_open):
        """测试边界：小表格到中等表格 (10行 -> 11行)"""
        mock_parser = MagicMock()

        # Test exactly at threshold (10 rows - small)
        mock_wb = MagicMock()
        mock_wb.sheetnames = ['Sheet1']
        mock_parser._load_excel_to_workbook.return_value = mock_wb
        mock_parser.get_sheet_row_count.return_value = 10
        mock_parser.get_sheet_row_chunks.return_value = ["row" + str(i) for i in range(10)]

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        blocks = adapter.parse('test.xlsx')
        assert len(blocks) == 10  # 逐行
        assert all(b.metadata["strategy"] == "row_by_row" for b in blocks)

        # Test just above threshold (11 rows - medium)
        mock_parser.get_sheet_row_count.return_value = 11
        mock_parser.get_sheet_markdown.return_value = "| markdown |"

        blocks = adapter.parse('test.xlsx')
        assert len(blocks) == 1  # 单个 Markdown
        assert blocks[0].metadata["strategy"] == "full_markdown"

    @patch('builtins.open')
    def test_boundary_medium_to_large(self, mock_open):
        """测试边界：中等表格到大表格 (200行 -> 201行)"""
        mock_parser = MagicMock()

        mock_wb = MagicMock()
        mock_wb.sheetnames = ['Sheet1']
        mock_parser._load_excel_to_workbook.return_value = mock_wb

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        # Test exactly at threshold (200 rows - medium)
        mock_parser.get_sheet_row_count.return_value = 200
        mock_parser.get_sheet_markdown.return_value = "| markdown |"

        blocks = adapter.parse('test.xlsx')
        assert len(blocks) == 1
        assert blocks[0].metadata["strategy"] == "full_markdown"

        # Test just above threshold (201 rows - large)
        mock_parser.get_sheet_row_count.return_value = 201
        mock_parser.get_sheet_html_chunks.return_value = [
            {"html": "<table>chunk1</table>", "row_range": "1-128", "chunk_index": 0, "chunk_total": 2},
            {"html": "<table>chunk2</table>", "row_range": "129-201", "chunk_index": 1, "chunk_total": 2},
        ]

        blocks = adapter.parse('test.xlsx')
        assert len(blocks) == 2
        assert all(b.metadata["strategy"] == "html_chunk" for b in blocks)

    @patch('builtins.open')
    def test_multiple_sheets(self, mock_open):
        """测试多 Sheet 文件"""
        mock_parser = MagicMock()

        mock_wb = MagicMock()
        mock_wb.sheetnames = ['SmallSheet', 'LargeSheet']
        mock_parser._load_excel_to_workbook.return_value = mock_wb

        def mock_row_count(wb, sheet_name):
            return 5 if sheet_name == 'SmallSheet' else 300

        mock_parser.get_sheet_row_count.side_effect = mock_row_count
        mock_parser.get_sheet_row_chunks.return_value = ["small row"]
        mock_parser.get_sheet_html_chunks.return_value = [
            {"html": "<table>large</table>", "row_range": "1-300", "chunk_index": 0, "chunk_total": 1}
        ]

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        blocks = adapter.parse('test.xlsx')

        # Should have 1 from SmallSheet + 1 from LargeSheet = 2 total
        assert len(blocks) == 2
        assert blocks[0].metadata["sheet_name"] == "SmallSheet"
        assert blocks[0].metadata["strategy"] == "row_by_row"
        assert blocks[1].metadata["sheet_name"] == "LargeSheet"
        assert blocks[1].metadata["strategy"] == "html_chunk"

    @patch('builtins.open')
    def test_empty_sheet_skipped(self, mock_open):
        """测试空 Sheet 被跳过"""
        mock_parser = MagicMock()

        mock_wb = MagicMock()
        mock_wb.sheetnames = ['EmptySheet', 'DataSheet']
        mock_parser._load_excel_to_workbook.return_value = mock_wb

        def mock_row_count(wb, sheet_name):
            return 0 if sheet_name == 'EmptySheet' else 10

        mock_parser.get_sheet_row_count.side_effect = mock_row_count
        mock_parser.get_sheet_row_chunks.return_value = ["data row"]

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        blocks = adapter.parse('test.xlsx')

        # Only DataSheet should produce blocks
        assert len(blocks) == 1
        assert blocks[0].metadata["sheet_name"] == "DataSheet"

    def test_custom_config_thresholds(self, custom_adapter):
        """测试自定义配置阈值"""
        assert custom_adapter.config.small_table_threshold == 5
        assert custom_adapter.config.large_table_threshold == 15
        assert custom_adapter.config.chunk_rows == 10

    @patch('builtins.open')
    def test_metadata_structure(self, mock_open):
        """测试元数据结构完整性"""
        mock_parser = MagicMock()

        mock_wb = MagicMock()
        mock_wb.sheetnames = ['Sheet1']
        mock_parser._load_excel_to_workbook.return_value = mock_wb
        mock_parser.get_sheet_row_count.return_value = 5
        mock_parser.get_sheet_row_chunks.return_value = ["row1"]

        mock_open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'fake')))
        mock_open.return_value.__exit__ = Mock(return_value=False)

        adapter = ExcelParserAdapter()
        adapter.ragflow_parser = mock_parser

        blocks = adapter.parse('test.xlsx')

        block = blocks[0]
        assert "sheet_name" in block.metadata
        assert "row_range" in block.metadata
        assert "total_rows" in block.metadata
        assert "chunk_index" in block.metadata
        assert "chunk_total" in block.metadata
        assert "strategy" in block.metadata
