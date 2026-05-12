# Parity 黄金样本（fixtures）

本目录用于存放 **RAGFlow 同构回归** 可选的离线样本（与 `tests/parity/test_*.py` 配合）。  
当前多数 parity 用例为**纯逻辑断言**，不强制依赖二进制文件；后续若增加「端到端文件级」对账，请将样本放在此目录或子目录中。

## 覆盖矩阵（建议）

| 类型 | 最小样本关注点 |
|------|------------------|
| pdf | 文本型 / 扫描件 / 多栏 / 含表 / 位置标签 |
| doc / docx | 标题层级、表格、旧版 doc 转换路径 |
| xlsx / xls / csv | 小/中/大表、多 sheet |
| md / mdx | 标题、表格、MDX 组件边界 |
| html / htm | 抽取文本与偏移稳定性 |
| json / jsonl / ldjson | 单行 JSON、非法行降级 |
| txt / py / js | 纯文本段落与 char span |
| epub | 分章结构 |

## 约束

- 仅使用**脱敏、可公开**的文档；勿提交真实客户数据或凭证。
- 大文件优先使用 LFS 或文档内说明「从何处生成」，避免仓库体积膨胀。

## 与测试的关联

- 路由与策略：`test_parser_routing_parity.py`
- 块映射：`test_parser_output_parity.py`
- 语义切片：`test_chunk_semantic_parity.py`
- 元数据字段：`test_chunk_metadata_parity.py`
- 编排参数：`test_orchestration_parity.py`
