# Excel 智能切片优化实施计划

## 概述

根据设计文档 `2026-04-09-excel-chunking-design.md`，实现 Excel 智能切片功能。

## 实施步骤

### 步骤 1: 创建 Excel 切片配置类

**文件**: `OpenRag/src/openrag/chunking/excel_config.py`

**动作**:
- 创建 `ExcelChunkingConfig` 数据类
- 包含字段: `small_table_threshold`, `large_table_threshold`, `chunk_rows`
- 添加配置验证逻辑

**验收标准**:
- 配置类可正确实例化
- 默认值符合设计: 10, 200, 256
- 支持自定义配置

---

### 步骤 2: 增强 RAGFlow ExcelParser

**文件**: `OpenRag/src/openrag/parsers/ragflow/parser/excel_parser.py`

**动作**:
- 确保 `html()` 方法支持 `chunk_rows` 参数
- 添加 `get_sheet_row_count()` 方法获取单个 Sheet 行数
- 添加 `get_sheet_markdown()` 方法获取单个 Sheet 的 Markdown

**验收标准**:
- 可独立获取每个 Sheet 的行数
- 可为单个 Sheet 生成 Markdown
- HTML 分块支持自定义 chunk_rows

---

### 步骤 3: 重写 Excel 适配器

**文件**: `OpenRag/src/openrag/parsers/adapters/excel_adapter.py`

**动作**:
- 修改 `__init__` 接受 `ExcelChunkingConfig` 参数
- 重写 `parse()` 方法实现智能切片逻辑
- 添加 `_parse_small_sheet()`: 逐行解析
- 添加 `_parse_medium_sheet()`: Markdown 完整解析
- 添加 `_parse_large_sheet()`: HTML 分块解析
- 添加 `_build_metadata()`: 构建 chunk 元数据

**验收标准**:
- 行数 ≤ 10: 逐行切片
- 行数 11-200: 单个 Markdown chunk
- 行数 > 200: HTML 分块，每 256 行一块
- 每个 chunk 包含 sheet_name, row_range, total_rows, chunk_index, chunk_total
- 多 Sheet 文件每个 Sheet 独立处理

---

### 步骤 4: 更新工厂类支持配置传递

**文件**: `OpenRag/src/openrag/parsers/factory.py`

**动作**:
- 修改 `get_parser()` 支持传递配置参数
- 确保 Excel 适配器能接收到配置

**验收标准**:
- 工厂类可传递配置到 Excel 适配器
- 不传递配置时使用默认值

---

### 步骤 5: 编写单元测试

**文件**: `OpenRag/tests/test_excel_chunking.py`

**动作**:
- 测试配置类默认值和自定义值
- 测试小表格 (≤10行) 逐行切片
- 测试中等表格 (11-200行) Markdown 切片
- 测试大表格 (>200行) HTML 分块
- 测试边界值: 10行、11行、200行、201行
- 测试多 Sheet 文件
- 测试元数据完整性

**验收标准**:
- 所有测试用例通过
- 覆盖率 > 90%

---

### 步骤 6: 编写集成测试

**文件**: `OpenRag/tests/test_excel_chunking_integration.py`

**动作**:
- 使用真实 Excel 文件测试
- 测试 CSV 文件处理
- 测试工厂类集成

**验收标准**:
- 真实文件解析正确
- 配置传递正确

---

### 步骤 7: 运行现有测试确保兼容性

**动作**:
- 运行 `test_ragflow_parser.py`
- 运行 `test_parser_factory.py`
- 运行 `test_chunk_engine.py`

**验收标准**:
- 所有现有测试通过
- 无破坏性变更

---

## 依赖关系

```
步骤 1 (配置类)
    ↓
步骤 2 (RAGFlow增强)
    ↓
步骤 3 (适配器重写) ← 步骤 1
    ↓
步骤 4 (工厂更新)
    ↓
步骤 5 (单元测试)
    ↓
步骤 6 (集成测试)
    ↓
步骤 7 (兼容性测试)
```

## 风险与缓解

| 风险 | 缓解措施 |
|-----|---------|
| RAGFlow 方法不兼容 | 先验证现有方法，必要时添加新方法 |
| 大文件内存问题 | 使用流式读取，测试大文件性能 |
| 破坏性变更 | 保持默认行为兼容，配置可选 |

## 时间估计

- 步骤 1: 15 分钟
- 步骤 2: 30 分钟
- 步骤 3: 45 分钟
- 步骤 4: 15 分钟
- 步骤 5: 45 分钟
- 步骤 6: 30 分钟
- 步骤 7: 15 分钟

总计: ~3 小时
