"""Excel chunking configuration."""

from dataclasses import dataclass


@dataclass
class ExcelChunkingConfig:
    """Excel 切片配置

    根据表格行数智能选择切片策略：
    - 小表格 (≤ small_table_threshold): 逐行切片
    - 中等表格 (small_table_threshold+1 ~ large_table_threshold): 完整 Markdown
    - 大表格 (> large_table_threshold): HTML 分块，每 chunk_rows 行一块

    Attributes:
        small_table_threshold: 小表格阈值（行），默认 10
        large_table_threshold: 大表格阈值（行），默认 200
        chunk_rows: 大表格分块行数，默认 256
    """
    small_table_threshold: int = 10
    large_table_threshold: int = 200
    chunk_rows: int = 256

    def __post_init__(self):
        """验证配置值"""
        if self.small_table_threshold < 1:
            raise ValueError(f"small_table_threshold must be >= 1, got {self.small_table_threshold}")
        if self.large_table_threshold < self.small_table_threshold:
            raise ValueError(
                f"large_table_threshold ({self.large_table_threshold}) must be >= "
                f"small_table_threshold ({self.small_table_threshold})"
            )
        if self.chunk_rows < 1:
            raise ValueError(f"chunk_rows must be >= 1, got {self.chunk_rows}")
