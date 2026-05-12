"""Integration tests for Excel smart chunking with real files."""

import pytest
import pandas as pd
import tempfile
import os
from openpyxl import Workbook

from openrag.chunking.excel_config import ExcelChunkingConfig
from openrag.parsers.adapters.excel_adapter import ExcelParserAdapter
from openrag.parsers.factory import ParserFactory


class TestExcelChunkingIntegration:
    """Integration tests with real Excel files"""

    @pytest.fixture
    def temp_dir(self):
        """创建临时目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    def create_excel_file(self, path, sheet_configs):
        """创建测试 Excel 文件

        Args:
            path: 文件路径
            sheet_configs: dict of {sheet_name: row_count}
        """
        wb = Workbook()
        default_sheet = wb.active
        wb.remove(default_sheet)

        for sheet_name, row_count in sheet_configs.items():
            ws = wb.create_sheet(title=sheet_name)
            # 添加表头
            ws.cell(row=1, column=1, value="Name")
            ws.cell(row=1, column=2, value="Age")
            ws.cell(row=1, column=3, value="City")

            # 添加数据行
            for i in range(2, row_count + 1):
                ws.cell(row=i, column=1, value=f"Person_{i}")
                ws.cell(row=i, column=2, value=20 + i)
                ws.cell(row=i, column=3, value=f"City_{i % 10}")

        wb.save(path)

    def test_small_file_row_by_row(self, temp_dir):
        """测试小文件逐行切片（使用真实文件）"""
        file_path = os.path.join(temp_dir, "small.xlsx")
        self.create_excel_file(file_path, {"Sheet1": 6})  # 1行表头 + 5行数据

        adapter = ExcelParserAdapter()
        blocks = adapter.parse(file_path)

        # 5行数据应该产生5个blocks (Person_2 到 Person_6)
        assert len(blocks) == 5
        for block in blocks:
            assert block.metadata["strategy"] == "row_by_row"
            assert block.metadata["sheet_name"] == "Sheet1"
            assert "Person_" in block.text

    def test_medium_file_markdown(self, temp_dir):
        """测试中等文件 Markdown 切片"""
        file_path = os.path.join(temp_dir, "medium.xlsx")
        self.create_excel_file(file_path, {"Sheet1": 50})  # 50行数据

        adapter = ExcelParserAdapter()
        blocks = adapter.parse(file_path)

        # 应该产生1个 Markdown block
        assert len(blocks) == 1
        assert blocks[0].metadata["strategy"] == "full_markdown"
        assert "|" in blocks[0].text  # Markdown 表格格式
        assert "Person_" in blocks[0].text

    def test_csv_file(self, temp_dir):
        """测试 CSV 文件处理"""
        file_path = os.path.join(temp_dir, "data.csv")

        # 创建 CSV 文件
        df = pd.DataFrame({
            "Name": [f"Person_{i}" for i in range(1, 8)],
            "Age": [20 + i for i in range(1, 8)],
        })
        df.to_csv(file_path, index=False)

        adapter = ExcelParserAdapter()
        blocks = adapter.parse(file_path)

        # 7行数据，小于 small_table_threshold (10)，应该逐行
        assert len(blocks) == 7
        assert all(b.metadata["strategy"] == "row_by_row" for b in blocks)

    def test_factory_with_config(self, temp_dir):
        """测试工厂类传递配置"""
        file_path = os.path.join(temp_dir, "test.xlsx")
        self.create_excel_file(file_path, {"Sheet1": 8})  # 8行数据

        # 使用默认配置（阈值10）
        factory = ParserFactory()
        parser1 = factory.get_parser(file_path)
        blocks1 = parser1.parse(file_path)
        assert all(b.metadata["strategy"] == "row_by_row" for b in blocks1)

        # 设置自定义配置（阈值5）
        config = ExcelChunkingConfig(small_table_threshold=5, large_table_threshold=15)
        factory.set_excel_config(config)
        parser2 = factory.get_parser(file_path)
        blocks2 = parser2.parse(file_path)

        # 8行数据 > 5 且 <= 15，应该使用 Markdown
        assert len(blocks2) == 1
        assert blocks2[0].metadata["strategy"] == "full_markdown"

    def test_factory_kwargs_config(self, temp_dir):
        """测试工厂类通过 kwargs 传递配置"""
        file_path = os.path.join(temp_dir, "test.xlsx")
        self.create_excel_file(file_path, {"Sheet1": 8})

        factory = ParserFactory()
        config = ExcelChunkingConfig(small_table_threshold=5, large_table_threshold=15)

        # 通过 kwargs 传递配置
        parser = factory.get_parser(file_path, excel_config=config)
        blocks = parser.parse(file_path)

        # 8行数据应该使用 Markdown
        assert len(blocks) == 1
        assert blocks[0].metadata["strategy"] == "full_markdown"

    def test_metadata_row_range(self, temp_dir):
        """测试元数据中的行范围"""
        file_path = os.path.join(temp_dir, "test.xlsx")
        self.create_excel_file(file_path, {"Sheet1": 6})  # 1行表头 + 5行数据

        adapter = ExcelParserAdapter()
        blocks = adapter.parse(file_path)

        # 逐行切片，每行应该有对应的 row_range
        for i, block in enumerate(blocks):
            # 数据行从1开始（不含表头）
            assert block.metadata["row_range"] == str(i + 1)
            assert block.metadata["total_rows"] == 6  # 1行表头 + 5行数据

    def test_multisheet_file(self, temp_dir):
        """测试多 Sheet 文件"""
        file_path = os.path.join(temp_dir, "multisheet.xlsx")
        self.create_excel_file(file_path, {
            "SmallSheet": 6,   # 1行表头 + 5行数据 = 小表格
            "LargeSheet": 250  # 大表格
        })

        adapter = ExcelParserAdapter()
        blocks = adapter.parse(file_path)

        # 找到不同 Sheet 的 blocks
        small_blocks = [b for b in blocks if b.metadata["sheet_name"] == "SmallSheet"]
        large_blocks = [b for b in blocks if b.metadata["sheet_name"] == "LargeSheet"]

        # 5行数据应该产生5个blocks
        assert len(small_blocks) == 5
        assert all(b.metadata["strategy"] == "row_by_row" for b in small_blocks)

        # 大表格应该分块
        assert len(large_blocks) >= 1
        assert all(b.metadata["strategy"] == "html_chunk" for b in large_blocks)

    def test_empty_sheet(self, temp_dir):
        """测试包含空 Sheet 的文件"""
        file_path = os.path.join(temp_dir, "test.xlsx")

        wb = Workbook()
        default_sheet = wb.active
        wb.remove(default_sheet)

        # 空 Sheet
        ws1 = wb.create_sheet(title="EmptySheet")
        ws1.cell(row=1, column=1, value="Header")  # 只有表头

        # 有数据的 Sheet
        ws2 = wb.create_sheet(title="DataSheet")
        ws2.cell(row=1, column=1, value="Name")
        ws2.cell(row=2, column=1, value="Alice")

        wb.save(file_path)

        adapter = ExcelParserAdapter()
        blocks = adapter.parse(file_path)

        # 只有 DataSheet 应该产生 blocks
        assert len(blocks) >= 1
        assert all(b.metadata["sheet_name"] == "DataSheet" for b in blocks)

    def test_realistic_data(self, temp_dir):
        """测试真实场景数据"""
        file_path = os.path.join(temp_dir, "employees.xlsx")

        # 创建更真实的数据
        wb = Workbook()
        ws = wb.active
        ws.title = "Employees"

        headers = ["ID", "Name", "Department", "Salary", "JoinDate"]
        for col, header in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=header)

        departments = ["Engineering", "Sales", "Marketing", "HR"]
        for i in range(2, 52):  # 50行数据
            ws.cell(row=i, column=1, value=i - 1)
            ws.cell(row=i, column=2, value=f"Employee_{i}")
            ws.cell(row=i, column=3, value=departments[i % 4])
            ws.cell(row=i, column=4, value=50000 + (i * 1000))
            ws.cell(row=i, column=5, value=f"2023-{i % 12 + 1:02d}-01")

        wb.save(file_path)

        adapter = ExcelParserAdapter()
        blocks = adapter.parse(file_path)

        # 50行数据应该产生单个 Markdown
        assert len(blocks) == 1
        block = blocks[0]
        assert block.metadata["strategy"] == "full_markdown"
        assert "Engineering" in block.text or "Sales" in block.text
        assert block.metadata["total_rows"] == 51  # 50 + 1 header
