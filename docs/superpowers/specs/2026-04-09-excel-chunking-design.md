# Excel 智能切片优化设计

## 概述

优化 OpenRag 的 Excel 切片策略，参考 RAGFlow 实现，根据表格行数智能选择不同的切片策略。

## 当前问题

- OpenRag 目前仅使用 RAGFlow 的 `__call__` 方法，逐行生成 chunk
- 对于小表格，逐行切片过于细碎；对于大表格，缺少分块机制

## 设计方案

### 切片策略

每个 Sheet 独立判断和处理：

| Sheet 行数 | 策略 | 输出格式 |
|-----------|------|---------|
| ≤ `small_table_threshold` (默认 10) | 逐行切片 | 每行一个 chunk，`列名: 值` 格式 |
| ≤ `large_table_threshold` (默认 200) | 完整表格 | 单个 Markdown 表格 |
| > `large_table_threshold` | 分块切片 | HTML 表格，每 `chunk_rows` (默认 256) 行一块 |

### 配置项

```python
@dataclass
class ExcelChunkingConfig:
    small_table_threshold: int = 10    # 小表格阈值（行）
    large_table_threshold: int = 200   # 大表格阈值（行）
    chunk_rows: int = 256              # 大表格分块行数
```

### Chunk 元数据

每个 chunk 包含以下 metadata：

- `sheet_name`: Sheet 名称
- `row_range`: 行范围（如 "1-10" 或 "257-512"）
- `total_rows`: Sheet 总行数
- `chunk_index`: 当前块序号（大表格分块时，从 0 开始）
- `chunk_total`: 总块数（大表格分块时）

### 实现变更

#### 1. 新增配置模型

文件：`openrag/src/openrag/chunking/excel_config.py`

```python
@dataclass
class ExcelChunkingConfig:
    """Excel 切片配置"""
    small_table_threshold: int = 10
    large_table_threshold: int = 200
    chunk_rows: int = 256
```

#### 2. 修改 Excel 适配器

文件：`openrag/src/openrag/parsers/adapters/excel_adapter.py`

- 新增 `ExcelParserAdapter.__init__(self, config: ExcelChunkingConfig = None)`
- 重写 `parse()` 方法实现智能切片逻辑
- 新增辅助方法：
  - `_parse_small_sheet()`: 逐行解析
  - `_parse_medium_sheet()`: Markdown 完整解析
  - `_parse_large_sheet()`: HTML 分块解析

#### 3. RAGFlow ExcelParser 增强（如有必要）

文件：`openrag/src/openrag/parsers/ragflow/parser/excel_parser.py`

- 确保 `html()` 方法支持 `chunk_rows` 参数
- 确保可以获取单个 Sheet 的行数而不解析全部内容

### 代码示例

```python
def parse(self, file_path: str) -> list[DocumentBlock]:
    """智能解析 Excel，根据行数选择策略"""
    wb = self._load_workbook(file_path)
    blocks = []

    for sheet_name in wb.sheetnames:
        row_count = self._get_sheet_row_count(wb, sheet_name)

        if row_count <= self.config.small_table_threshold:
            # 小表格：逐行
            blocks.extend(self._parse_small_sheet(wb, sheet_name))
        elif row_count <= self.config.large_table_threshold:
            # 中等表格：完整 Markdown
            blocks.extend(self._parse_medium_sheet(wb, sheet_name))
        else:
            # 大表格：分块 HTML
            blocks.extend(self._parse_large_sheet(wb, sheet_name))

    return blocks
```

## 边界情况处理

1. **空 Sheet**: 跳过，不生成 chunk
2. **只有表头**: 按小表格处理（逐行，但只有表头一行）
3. **CSV 文件**: 视为单 Sheet Excel 处理
4. **超大文件**: 使用流式读取避免内存问题

## 测试策略

1. 单元测试：三种阈值边界（9行、10行、11行、199行、200行、201行）
2. 单元测试：多 Sheet 文件
3. 单元测试：配置项覆盖默认值
4. 集成测试：真实 Excel 文件解析

## 验收标准

- [ ] 行数 ≤ 10 的 Sheet 逐行切片
- [ ] 行数 11-200 的 Sheet 生成单个 Markdown chunk
- [ ] 行数 > 200 的 Sheet 按 256 行分块生成 HTML chunks
- [ ] 每个 chunk 包含 sheet_name、row_range 等元数据
- [ ] 配置项可通过参数自定义
- [ ] 多 Sheet 文件每个 Sheet 独立处理
