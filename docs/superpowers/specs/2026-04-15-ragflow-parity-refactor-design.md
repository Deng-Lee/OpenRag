# OpenRag 解析与切片同构 RAGFlow 重构设计

## 背景与目标

当前 OpenRag 已引入部分 RAGFlow 解析/切片能力，但仍存在以下问题：
- 文件类型路由与策略分发未完全按 `@ragflow` 对齐；
- 解析与切片逻辑中仍有 OpenRag 自定义分支，导致行为不一致；
- 对外模型契约（`DocumentBlock`/`Chunk`）与内部策略实现耦合，难以验证同构性。

本设计目标：
- 以 `@ragflow` 为唯一基线，重构 OpenRag 解析与切片逻辑；
- 内部实现严格同构，外部保持 OpenRag 现有接口兼容；
- 支持可验证、可观测、可回归的上线路径。

约束：
- 采用“同构 + 兼容层”策略；
- 一次性全量覆盖已支持文档类型；
- 不在本阶段引入与目标无关的重构。

## 方案选择

已选方案：**A. 分层镜像迁移（推荐）**

对比结论：
- 相比原地替换，A 方案回归风险更低、边界更清晰；
- 相比双轨运行，A 方案复杂度可控，仍保留足够的对账能力；
- 符合“严格同构 + 兼容输出”的核心诉求。

## 总体架构

采用三层结构：

1. `ragflow_core`（新增，内部同构层）
- 承载与 `@ragflow` 对齐的解析与切片策略；
- 禁止掺入 OpenRag 业务特化逻辑；
- 负责策略分发、回退链、chunk 语义生成。

2. `compat_adapters`（新增/重构，兼容映射层）
- 将 `ragflow_core` 原生输出映射为 OpenRag 现有模型；
- 补齐兼容字段，不改策略决策；
- 保证下游（embedding/ES/Milvus/层级存储）可无感接入。

3. `orchestration`（现有编排层，最小变更）
- 以 `DocumentProcessor` 为主，继续负责流程状态机与生命周期编排；
- 不再承载文件类型特化策略，仅调用统一入口。

建议目录边界：
- `openrag/ragflow_core/`（解析策略与路由；置于 `parsers` 包外以避免导入副作用）
- `openrag/chunking/ragflow_core/`
- `openrag/parsers/adapters/`（保留路径，内部重构为 compat 适配）
- `openrag/chunking/chunk_engine.py`（壳层委托）

## 组件与数据流

### 解析链路
- `DocumentProcessor.process_document()` -> `ParserRegistry` -> `ParserFactory`；
- `ParserFactory` 路由到 `ragflow_core` 策略封装；
- `ragflow_core` 执行真实解析并产出原生结构；
- `compat_adapters` 映射为 `DocumentBlock`（保留 `page/bbox/block_type/layout_type/char span`）。

### 切片链路
- `ChunkEngine.chunk()` 保持现有签名；
- 内部委托 `chunking/ragflow_core` 执行同构切片：
  - naive merge；
  - docx-like merge；
  - children delimiter；
  - table/image context；
  - ragflow metadata 生成。
- 输出映射为 OpenRag `Chunk`，并保留 ragflow 兼容 metadata 字段。

### 存储链路
- embedding、Milvus、ES、层级构建流程不做架构改写；
- 通过兼容层保证 `Chunk` 主字段和下游索引字段可持续消费。

## 文件类型策略对齐矩阵

| 类型 | 同构策略（基于 `@ragflow`） | OpenRag 兼容映射要求 |
|---|---|---|
| pdf | 按 ragflow PDF 策略分发与回退顺序执行（含多后端链路） | 保留位置语义，映射为稳定 `DocumentBlock` 与 metadata |
| docx | 按 ragflow docx 主链解析与合并规则 | 标题层级映射 `level`，表格映射 `block_type=table` |
| doc | 按 ragflow fallback 链执行 | 对外保持 Word 文件可解析的兼容行为 |
| xlsx/xls/csv | 按 ragflow Excel 策略（行级/整表/分块） | 保留 sheet、row_range、chunk_index 等 metadata |
| txt/py/js/... | 归并到 ragflow 文本类解析链 | 统一 text block，保留段落/offset 语义 |
| md/markdown/mdx | 按 ragflow markdown 解析与分段 | 标题/表格语义保持一致 |
| html/htm | 按 ragflow html 解析链 | 抽取文本流后映射稳定 offset |
| epub | 按 ragflow epub 解析 | 章节信息映射为可检索 metadata |
| json/jsonl/ldjson | 按 ragflow json 类解析链 | 记录级输出并补齐索引字段 |

## 切片同构设计

### 核心原则
- `ChunkEngine` 仅作为兼容入口；
- 策略判断、分割与合并均在 `ragflow_core` 内完成；
- 兼容层不得覆盖或重写策略结果。

### 同构能力范围
1. 文本 naive merge：
- delimiter/custom delimiter 行为一致；
- token 计数、overlap、strict limit 一致。

2. docx-like 混合块：
- `text/table/image` 分类型处理；
- `_merge_cks` 合并规则一致；
- table/image 上下文提取一致。

3. children delimiter：
- `split_with_pattern` 行为一致；
- 子块补齐 `mom_with_weight` 与 token 字段。

4. 位置与检索字段：
- 统一补齐 `page_num_int/position_int/top_int/doc_type_kwd`；
- 无真实坐标时按 ragflow 退化规则填充。

## 错误处理与可观测性

### 错误分层
- `ragflow_core`：策略/后端错误；
- `compat_adapters`：结构映射错误；
- `DocumentProcessor`：流程状态与对外错误输出。

### 错误分类（示例）
- `PARSER_UNSUPPORTED_FORMAT`
- `PARSER_BACKEND_UNAVAILABLE`
- `PARSER_FALLBACK_EXHAUSTED`
- `CHUNK_STRATEGY_FAILED`
- `COMPAT_MAPPING_FAILED`

### 可观测要求
- 回退链路必须结构化日志记录：`file_id/ext/from->to/reason/elapsed_ms`；
- 引入 parity 对账日志（仅调试/灰度）：
  - chunk 数；
  - token 分布；
  - table/image 比例；
  - metadata 覆盖率。

## 测试策略与验收标准

### 测试分层
- 单元测试：策略函数级同构验证；
- 适配测试：core 到 OpenRag 模型映射；
- 端到端测试：上传到入库全链路；
- 回归测试：固定样本集差异对账。

### 黄金样本覆盖
- pdf、doc/docx、xlsx/xls/csv、md/mdx、html、json/jsonl/ldjson、txt/py/js、epub；
- 覆盖文本、表格、图片、扫描件、乱码风险与边界输入。

### Parity Gate
- 硬性一致（100%）：
  - 策略分发路径；
  - 关键 metadata 字段存在性；
  - children delimiter 开关行为。
- 阈值一致（可配置）：
  - token 误差 <= 3%；
  - chunk 数偏差 <= 5%（白名单类型）。

### Done 标准
- 所有支持类型切换至 `ragflow_core` 同构路径；
- OpenRag 对外契约保持兼容；
- parity 报告通过，无硬性不一致；
- 全链路可在真实样本集跑通。

## 实施里程碑

1. 搭建 `ragflow_core` 与 `compat_adapters` 骨架；
2. 完成文件路由全量对齐；
3. 完成切片同构迁移；
4. 接入对账日志与 parity 测试；
5. 端到端回归与上线验收。

## 风险与应对

- 风险：同构过程中混入旧逻辑导致“伪一致”；
  - 应对：策略层禁止兼容分支，兼容层仅做映射。
- 风险：样本不足导致上线后偏差暴露；
  - 应对：先扩充黄金样本，再放行 parity gate。
- 风险：定位成本高；
  - 应对：强制结构化日志与阶段性对账报告。

## 非目标

- 不在本阶段重构 embedding 或检索排序算法；
- 不引入与 ragflow 同构无关的 UI/接口扩展；
- 不对历史数据做一次性离线重算（另行规划）。
