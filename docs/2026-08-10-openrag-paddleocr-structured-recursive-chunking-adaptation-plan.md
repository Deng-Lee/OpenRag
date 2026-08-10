# OpenRAG PaddleOCR 结构感知递归切块适配方案

> 日期：2026-08-10
>
> 目标分支：`fix-rank-bug`
>
> 适用范围：公开 `parser_type=auto/pdf` 所使用的 PaddleOCR PDF 链路、`document_type=general`、现有 `ChunkStrategy.SEMANTIC` 内部实现
>
> 前置方案：`docs/2026-08-10-openrag-deepdoc-structured-recursive-chunking-implementation-plan.md`
>
> 核心约束：保持现有 PaddleOCR 模型、业务 OCR 校正和远端接口不变；不新增公开 `ChunkStrategy`；复用已经为 DeepDoc 实现的标题 Resolver 和结构递归 Chunker。

## 1. 方案目标

当前 `fix-rank-bug` 分支已经为显式 `parser_type=deepdoc` 的 PDF 实现：

1. DeepDoc 有序结构化 Block 转换；
2. `PdfHeadingHierarchyResolver` 标题候选和层级解析；
3. 可信 H1 硬边界；
4. H1 区域内递归分块；
5. 无可信 H1 时全文递归兜底；
6. `ChunkEngine` 内部路由和回退开关。

但生产默认的 `auto/pdf` 仍使用 `PaddleOCRPDFParserAdapter`。当前 PaddleOCR Adapter 虽然收到了标题、正文、表格、页码、bbox 等结构化信息，却仍将除表格外的大部分内容转换为 `text / level=0`，因此无法进入已经实现的结构感知递归链路。

本方案的目标不是再实现一套 PaddleOCR 专属分块器，而是完成以下适配：

```text
PaddleOCR 异步结果
  -> PaddleOCR Adapter 归一化有序 Block 和版面证据
  -> 通用 PdfHeadingHierarchyResolver
  -> 统一的 heading/path/hard_boundary 语义
  -> 现有 GeneralStructuredRecursiveChunker
  -> Chunk 保留页码、bbox、来源 Block 和标题路径
```

完成后应满足：

- `doc_title` 被识别为文档标题，但永远不作为 H1 硬边界；
- `paragraph_title/title` 成为标题候选，而不是直接成为 H1；
- Outline、标题编号、PaddleOCR Markdown 层级、版面置信度和几何样式可以共同参与 H1/H2/H3 推断；
- 表格继续保持独立类型和原始阅读顺序；
- 页码、页眉、页脚等噪声不进入 Chunk；
- 只有可信 H1 才能切分结构区域；
- 未识别到可信 H1 时使用全文递归兜底；
- `auto/pdf` 的 Parser 选择、PaddleOCR 服务协议和失败行为不变；
- `manual/laws`、非 PDF 和公开 `ChunkStrategy` 不进入新的 General 结构递归路由。

## 2. 当前状态与实测依据

### 2.1 当前公开 Parser 契约

当前仓库约定：

- `auto + PDF`：使用 `PaddleOCRPDFParserAdapter`，有效公开类型为 `pdf`；
- `parser_type=pdf`：显式使用 PaddleOCR；
- `parser_type=deepdoc`：显式使用原有 DeepDoc；
- 旧公开值 `paddleocr` 已移除；
- PaddleOCR 失败时当前任务失败，不自动降级到 DeepDoc；
- 两种 PDF Parser 的 `chunk_method` 都是 `pdf_manual`。

需要特别区分：`pdf_manual` 是传给 ChunkEngine 的 `chunk_method`，不是 `document_type`。本方案仍只在 `document_type=general` 时启用结构递归；`manual/laws` 使用原有 document profile 路径。

### 2.2 33 节点 PaddleOCR 实测

2026-08-10 在 33 测试节点使用两页小型 PDF 完成实测。文档包含：

- 一个文档标题；
- 两个一级章节标题；
- 四个二级标题；
- 七个正文 Block；
- 一个表格；
- 两个页码。

任务 `269c62a6387f4966` 在约 `1.518s` 内完成，`parsing_res_list` 返回：

| label | 数量 | 观察结果 |
|---|---:|---|
| `doc_title` | 1 | 正确识别文档标题 |
| `paragraph_title` | 6 | 两个 H1 和四个 H2 都使用同一个 label |
| `text` | 7 | 正文按版面块返回 |
| `table` | 1 | 保留为独立表格，并返回 HTML |
| `number` | 2 | 页脚页码被单独识别 |

标题版面分数位于 `layout_det_res.boxes`，本次范围为 `0.8428～0.9073`。服务生成的 Markdown 正确给出：

```text
#  文档标题
## 第一章 / 第二章
### 1.1 / 1.2 / 2.1 / 2.2
```

实测结论：

1. 当前模型足以提供标题候选、版面类型、内容、页码、bbox、polygon、置信度和基础顺序；
2. `parsing_res_list` 没有直接提供 H1/H2/H3，一级和二级标题都是 `paragraph_title`；
3. `layout_det_res.boxes` 和 `parsing_res_list` 分离，当前 Adapter 没有把置信度关联回文本 Block；
4. Markdown 中存在可利用的相对标题深度，但缺少稳定 Block ID，不能单独作为硬边界依据；
5. OCR 文本仍可能出现单字误识别，本次“递归切块”曾被识别为“递归切换”，标题结构适配不能替代 OCR 准确率治理。

这是一份小样本的接口能力确认，不代表复杂扫描件、多栏、目录页和异常版式已经达到生产精度；这些场景必须进入后续金标验证。

### 2.3 当前 Adapter 丢失的信息

当前 `paddleocr_pdf_adapter.py::_result_to_rows()` 的关键行为是：

```python
label = item.get("label", "text")
block_type = "table" if label == "table" else "text"
level = 0
layout_type = block_type
metadata = {"ocr_label": label}
```

因此当前存在以下问题：

- `doc_title`、`paragraph_title` 和 `number` 都成为普通文本；
- 标题统一为 `level=0`；
- `layout_det_res.score/order` 没有进入 `DocumentBlock`；
- Markdown 标题层级没有被消费；
- 页码可能进入 Chunk；
- `compat_source=paddleocr`，不满足当前 ChunkEngine 中 DeepDoc 专属的 `compat_source==pdf` 条件；
- PaddleOCR 无法复用已经完成的结构递归 Chunker。

## 3. 总体设计与模块 seam

本方案使用三个 seam 隔离不同复杂度：

```text
┌──────────────────────────────────────────────┐
│ PaddleOCRPDFParserAdapter                   │
│ 原始远端结果 -> 统一、有序 DocumentBlock     │
└──────────────────────┬───────────────────────┘
                       │ DocumentBlock interface
┌──────────────────────▼───────────────────────┐
│ PdfHeadingHierarchyResolver                 │
│ 候选 + 多证据 -> level/path/hard_boundary   │
└──────────────────────┬───────────────────────┘
                       │ 已确认结构 interface
┌──────────────────────▼───────────────────────┐
│ GeneralStructuredRecursiveChunker           │
│ H1 区域 -> 递归分块 -> Chunk                 │
└──────────────────────────────────────────────┘
```

职责约束：

- Adapter 知道 PaddleOCR 响应格式，但不独立发明标题层级规则；
- Resolver 知道标题证据、置信度和层级关系，但不知道远端 HTTP 协议；
- Chunker 只消费已经确认的 `level/hard_boundary/heading_path`，不知道 DeepDoc 或 PaddleOCR；
- `DocumentProcessor` 继续只负责解析、artifact、分块、Embedding 和持久化编排；
- ParserFactory 继续决定 `auto/pdf/deepdoc` 选择，不参与结构推断。

这样可以直接复用 DeepDoc 后两层，并把 PaddleOCR 特有逻辑限制在 Adapter seam 内。

## 4. 统一 DocumentBlock 语义

PaddleOCR 归一化后使用现有 `DocumentBlock`，不新增公共 PDF AST。

### 4.1 标题候选阶段

尚未确认层级的标题候选应表示为：

```python
DocumentBlock(
    text="第一章 系统概述",
    page=1,
    bbox=(...),
    block_type="text",
    layout_type="title",
    level=0,
    metadata={
        "structured_pdf": True,
        "parser_backend": "paddleocr",
        "ocr_label": "paragraph_title",
        "heading_candidate": True,
        "layout_score": 0.88,
        "paddleocr_order": 2,
        "style_source": "paddle_bbox",
        "geometry_height_ratio": 0.024,
        "geometry_left_ratio": 0.12,
    },
)
```

约束：

- 候选阶段仍是 `block_type=text、level=0`；
- `layout_type=title` 只表达版面模型认为它像标题；
- `paragraph_title` 不能直接映射为 H1；
- 缺失置信度时字段保持缺失，不得写成 `0`；
- 几何高度是 bbox 代理量，不得伪装成真实字体大小。

### 4.2 文档标题

`doc_title` 应表示为：

```python
metadata={
    "heading_candidate": True,
    "heading_role": "document_title",
    "hard_boundary": False,
    "ocr_label": "doc_title",
}
```

无论它是否带编号、字号多大、是否匹配 Markdown `#`，都不得成为章节 H1。

### 4.3 已确认标题

Resolver 确认后复用 DeepDoc 的统一语义：

```python
DocumentBlock(
    block_type="heading",
    layout_type="title",
    level=1,
    metadata={
        "heading_role": "section",
        "heading_confidence": "hard",
        "heading_score": 0.95,
        "heading_sources": ["numbering", "paddle_markdown", "layout"],
        "parent_heading_id": None,
        "heading_path": ["第一章 系统概述"],
        "hard_boundary": True,
    },
)
```

### 4.4 非标题 Block

- `text`：普通正文，继承当前 `heading_path`；
- `table`：保留 HTML、bbox 和原始位置，作为原子 Block；
- `image/figure/chart`：服务返回时保留独立类型；
- `table_caption/figure_caption`：可保留为正文或 caption 类型，但永远不作为 H1；
- `number/header/footer/footnote`：从可分块正文流排除。

## 5. 六个实施步骤

### 5.1 步骤一：保留和归一化 PaddleOCR 结构类型

#### 当前解决的问题

当前 `_result_to_rows()` 只区分 `table` 和 `text`，使 `doc_title`、`paragraph_title`、页码等语义丢失。后续 Resolver 无法知道哪些 Block 是标题候选，也无法过滤页脚。

#### 处理方式

1. 继续以每页 `parsing_res_list` 作为主有序 Block 流；
2. 不使用 `layout_det_res.order` 重新排序，因为实测中表格的 `order` 可能为 `null`，而 `parsing_res_list` 已把表格插入正确位置；
3. 增加 Paddle label 到统一语义的映射：

| Paddle label | block_type | layout_type | 初始 level | 处理 |
|---|---|---|---:|---|
| `doc_title` | `text` | `title` | 0 | 文档标题候选，预设 `heading_role=document_title` |
| `paragraph_title/title` | `text` | `title` | 0 | 章节标题候选 |
| `text` | `text` | `text` | 0 | 正文 |
| `table` | `table` | `table` | 0 | 保留 HTML 和原子类型 |
| `image/figure/chart` | `image` | 原始 label | 0 | 服务有返回时保留 |
| caption/reference | `text` | 原始 label | 0 | 保留内容但排除标题候选 |
| number/header/footer/footnote | 不进入正文流 | 原始 label | 0 | 统计后过滤 |

4. 保留当前 bbox 从 OCR 图片坐标缩放到 PDF 页面坐标的逻辑；
5. `compat_source` 继续保持 `paddleocr`，另写 `parser_backend=paddleocr`，不伪装成 DeepDoc；
6. 输出 `structured_pdf=true`，表示已经获得可用于结构处理的有序 PDF Block。

#### 复用 DeepDoc 的内容

- 复用 `DocumentBlock` interface；
- 复用 `structured_pdf`、page/bbox/block_id/char span 等字段语义；
- 复用“Adapter 只负责原始结果归一化”的 seam；
- 不复用 `_deepdoc_boxes_to_document_blocks()` 的实现，因为两种原始结果结构不同。

#### 行为边界

- 不修改 PaddleOCR HTTP 提交、轮询、超时和失败逻辑；
- 不修改 OCR 模型和业务校正；
- 不在本步骤推断 H1/H2/H3；
- 不重新排序 `parsing_res_list`；
- 不新增行级 `line_positions`，点击回源继续使用 block 级 bbox；
- 空内容 Block 继续跳过；无效 bbox 继续保留文字但丢弃坐标，与当前降级语义一致。

#### 影响模块/函数

- `openrag/src/openrag/parsers/adapters/paddleocr_pdf_adapter.py`
  - `PaddleOCRPDFParserAdapter.parse()`
  - `_result_to_rows()`
  - 可新增私有 `_normalize_paddle_label()`
  - `_scale_bbox_to_pdf()` 保留，只补回归测试
- `openrag/src/openrag/ragflow_core/compat.py::to_document_blocks()` 原则上无须修改

#### 验证方式

- 输入 `doc_title -> paragraph_title -> text -> table -> text -> number`，输出顺序保持一致；
- `doc_title/paragraph_title` 不再丢失原始 label；
- `paragraph_title` 初始仍为 `level=0`；
- table 保留 HTML、bbox 和 `block_type=table`；
- number 不进入可分块正文；
- 现有 bbox 缩放、旋转、裁剪和无效 bbox 测试全部继续通过。

### 5.2 步骤二：把版面置信度和 Markdown 层级关联回 Block

#### 当前解决的问题

PaddleOCR 的文本和置信度位于两个结果集合：

```text
parsing_res_list              layout_det_res.boxes
label                         label
bbox                          coordinate
content                       score
                              order
```

当前 Adapter 只消费左侧，因此标题候选失去版面分数。Markdown 中虽有相对标题深度，也没有关联回 Block。

#### 处理方式

##### 版面框关联

在 OCR 原始像素坐标中匹配，避免先缩放产生额外误差：

```text
同一 page
  -> label 相同或属于允许的等价标签组
  -> bbox IoU / containment 达到阈值
  -> 单调顺序优先
  -> 选择最佳匹配
```

匹配结果写入：

```python
metadata["layout_score"]
metadata["paddleocr_layout_order"]
metadata["layout_match_iou"]
metadata["layout_match_count"]
```

由于 PaddleOCR 开启 `merge_layout_blocks` 后可能出现多个 Layout Box 合并为一个 Parsing Block：

- 一对一时使用最佳 IoU 匹配；
- 一对多时记录 `layout_match_count`，分数使用匹配集合的最大值；
- 无匹配时字段缺失，不阻止内容进入后续流程；
- Layout 的 `order` 只作为证据和诊断字段，不覆盖 `parsing_res_list` 顺序。

##### Markdown 标题关联

逐页解析 `markdown.markdown_texts` 中的 ATX 标题：

```text
^(#{1,6})\s+(.+)$
```

再按以下条件一对一匹配标题候选：

```text
page + 规范化文本相似度 + 单调顺序
```

匹配后保存：

```python
metadata["paddle_markdown_level_raw"]
metadata["paddle_markdown_match_score"]
```

全文完成匹配后再规范化层级：

- 有 `doc_title=#` 时，后续 `##/###` 相对转换为章节 H1/H2；
- 无文档标题时，以全文最浅的章节标题深度归一为 H1；
- 不匹配的 Markdown 标题不创建虚构 Block；
- Markdown 标题深度只是证据，不直接设置 `hard_boundary`。

#### 复用 DeepDoc 的内容

- 复用 `heading_score/heading_confidence/heading_sources` 的最终语义；
- 复用 Resolver 对版面弱证据的处理原则；
- 不复用 DeepDoc 的 score 传递实现，因为 DeepDoc score 已位于同一个 Box，而 PaddleOCR 需要跨结果集合匹配。

#### 行为边界

- 不要求修改远端 OCR 服务响应；
- 不以 score 低为由删除正文；
- 不以 Markdown 单证据制造 H1 硬边界；
- 不从 bbox 高度伪造 `font_size`；
- 不追求正文多框合并关系的完美还原，首期重点保证标题候选的置信度关联；
- 如果未来允许调整 OCR 服务封装，可直接把 score/order 写入 `parsing_res_list`，但不属于本方案必需项。

#### 影响模块/函数

- `paddleocr_pdf_adapter.py::_result_to_rows()`
- 可新增私有函数：
  - `_match_layout_evidence(page)`
  - `_extract_markdown_headings(page)`
  - `_match_markdown_headings(items, headings)`
  - `_bbox_iou()` / `_bbox_containment()`
- `PdfHeadingHierarchyResolver` 只消费归一化后的 evidence，不读取 Paddle 原始 JSON

#### 验证方式

- exact bbox、轻微偏移 bbox、一对多合并、无匹配分别有测试；
- 同页相同标题文本仍遵守一对一和单调顺序；
- 表格 `layout order=null` 不改变主 Block 顺序；
- `doc_title # + section ## + subsection ###` 正确得到相对 0/1/2 语义；
- 无 doc_title 且从 `#` 开始时不发生多减一级；
- Markdown 文本不匹配时不创建标题；
- Layout score 缺失时 Resolver 能继续使用 Outline、编号和几何证据。

### 5.3 步骤三：复用并扩展 PDF 标题层级 Resolver

#### 当前解决的问题

`paragraph_title` 只能说明“像标题”，不能区分 H1/H2/H3。直接把所有 `paragraph_title` 设为 H1 会产生大量错误硬边界。

#### 处理方式

继续使用现有 `PdfHeadingHierarchyResolver.resolve(blocks, outlines)` 作为唯一标题层级 interface，并扩展其输入证据。

##### 标题候选

候选来源包括：

- `layout_type=title`；
- `ocr_label=paragraph_title/title`；
- PDF Outline 文本匹配；
- 强标题编号；
- 已匹配的 Paddle Markdown 标题。

排除：

- table/image/figure；
- caption/reference；
- header/footer/number/footnote；
- 目录行和页码；
- 超长正文；
- 明显连续列表项。

`ocr_label=doc_title` 进入候选，但预先固定为 `heading_role=document_title、level=0、hard_boundary=false`。

##### PDF Outline

PaddleOCR 服务不返回 PDF Outline，但 Adapter 仍拥有原始 PDF 文件。新增原始 PDF Outline 提取，输出已有 `PdfOutlineEntry(text, level, page)`，继续复用：

- Outline 深度到 H1/H2/H3 的转换；
- page + 规范化文本相似度的一对一匹配；
- Outline 未命中后编号/样式回退；
- 未命中 Outline 不创建虚构 Block。

Outline 提取放在通用 PDF 标题模块内，避免 Paddle Adapter 导入 RAGFlow/DeepDoc 内部 Parser。

##### 全文编号体系

直接复用现有编号解析，并保证全文一致性：

- “第一章/第二章”、`Chapter 1` 等显式章节编号可形成 H1 强证据；
- `1.1/1.2` 推断 H2，`1.1.1` 推断 H3；
- 单独的 `1.` 只有在全文标题序列一致时才作为 H1；
- 连续列表、表格行号和页码不进入标题编号体系；
- 编号族冲突或层级跳跃时降低置信度。

##### Paddle Markdown 证据

规范化后的 Markdown 深度作为 `paddle_markdown` 证据：

- 与 Outline/编号一致时提高可信度；
- 与强 Outline 冲突时以 Outline 为准并记录冲突；
- 单独命中只能形成 soft/supported 标题；
- Markdown H1 若要成为硬边界，还需要强编号或全文一致的 H1 几何样式组支持。

##### 几何样式证据

PaddleOCR 没有可靠字体名、字号和粗体信息，只使用可从 bbox 推导的代理特征：

```text
geometry_height_ratio = bbox_height / page_height
geometry_left_ratio   = bbox_x0 / page_width
geometry_width_ratio  = bbox_width / page_width
top_gap_ratio         = 与前一个正文块的垂直间距 / page_height
bottom_gap_ratio      = 与后一个正文块的垂直间距 / page_height
center_offset_ratio   = Block 中心与页面中心的偏移
```

这些字段使用 `style_source=paddle_bbox`，只与同来源候选比较。它们可以继承已有强锚点的层级，但不能独立制造硬 H1。

##### 证据优先级与 H1 门控

建议优先级：

```text
Outline
  > 显式章节编号
  > 编号 + Markdown 一致
  > Markdown + 全文一致几何样式
  > 有锚点的几何样式组
  > paragraph_title/layout score 单证据
```

硬边界规则保持不变：

```python
hard_boundary = (
    heading_role == "section"
    and level == 1
    and heading_confidence in {"hard", "supported"}
)
```

其中：

- Outline H1、明确“第一章/Chapter 1”可以为 `hard`；
- Markdown H1 + 编号或一致样式可以为 `supported`；
- Markdown 单证据、几何单证据、`paragraph_title` 单证据只能为 `soft`；
- 强证据冲突时设为 `conflict` 或降为普通候选，不设置硬边界。

最后继续复用 `_assign_heading_paths()` 写入父标题和 `heading_path`。

#### 复用 DeepDoc 的内容

可以直接复用：

- `normalize_pdf_outlines()`；
- `_match_outlines()`；
- `_parse_numbering()`；
- `_confirm_heading()` 的统一输出语义；
- `_apply_style_anchors()` 的锚定思想；
- `_assign_heading_paths()`；
- `hard/supported/soft/conflict` 置信度和 H1 高精度原则。

需要扩展：

- `_candidate_indices()` 接受 Paddle label/metadata；
- `_style_value()` 接受 `geometry_height_ratio`；
- `_same_style_source()` 支持 `paddle_bbox`；
- Layout score 读取通用 `layout_score`，并兼容现有 `deepdoc_layout_score`；
- Resolver 接受 Markdown evidence；
- 对 `doc_title` 增加显式保护，而不是仅依赖“第一页第一个 Block”猜测。

#### 行为边界

- 不新增 PaddleOCR 专属 Resolver；
- 不用 LLM 判断标题层级；
- 不把所有 `paragraph_title` 提升为 heading；
- 不把文档标题作为 H1；
- 不在没有可靠证据时伪造层级；
- 不要求 PDF 必须有 Outline；
- Outline、Markdown 和 bbox 缺失时仍可使用编号，全部缺失时保留普通正文并交给全文兜底。

#### 影响模块/函数

- `openrag/src/openrag/parsers/pdf_heading_hierarchy.py`
  - `PdfHeadingHierarchyResolver.resolve()`
  - `_candidate_indices()`
  - `_match_outlines()` 复用并补回归测试
  - `_parse_numbering()` 复用并补 Paddle label 场景
  - `_apply_style_anchors()`
  - `_style_value()`
  - `_same_style_source()`
  - `_assign_heading_paths()` 原则上不改
  - 可新增 `extract_pdf_outlines(file_path)`
- `paddleocr_pdf_adapter.py::parse()`：在归一化后调用 Resolver
- `pdf_adapter.py` 原则上不改；现有 DeepDoc 输入必须通过回归测试

#### 验证方式

- `doc_title` 永远保持 `level=0、hard_boundary=false`；
- Outline H1/H2/H3 正确匹配 Paddle block；
- “第一章 + 1.1 + 1.1.1”得到 H1/H2/H3；
- H1/H2 都是 `paragraph_title` 时仍能通过证据分层；
- Markdown 与编号一致时形成 supported 层级；
- Markdown 与 Outline 冲突时 Outline 胜出且记录 conflict/source；
- 几何样式只能继承锚点，不能独立制造硬 H1；
- 无标题、无 Outline、无编号文档所有 Block 保持 level=0；
- DeepDoc 现有 Resolver 测试全部继续通过。

### 5.4 步骤四：过滤噪声并保持统一阅读顺序

#### 当前解决的问题

实测页脚被识别为 `number`，当前 Adapter 会将其转换为正文。重复页眉、页脚、页码和脚注会污染 Chunk、Embedding 和检索结果。另一方面，错误重排又可能破坏表格与正文的相对顺序。

#### 处理方式

1. 主顺序始终采用 `pages` 顺序和页内 `parsing_res_list` 顺序；
2. 不按 Y 坐标全量重排，不使用可能为空的 `layout order` 覆盖主顺序；
3. 明确非正文 label 集合：

```text
number
header
header_image
footer
footer_image
footnote
aside_text（首期默认排除，可按金标结果复核）
```

4. 这些 Block 不进入 canonical content 和 Chunk，但记录按 label 的过滤数量到 parse profile；
5. `table/figure/image` 保留在 `parsing_res_list` 的原位置；
6. caption/reference 保留内容，但在 Resolver 候选阶段排除；
7. 若同一页出现重复、重叠正文框，不在本步骤进行激进文本去重，避免丢字；只记录诊断指标，由金标结果决定是否另立问题。

#### 复用 DeepDoc 的内容

- 复用 Resolver 中页眉、页脚、页码、目录项和 caption 的候选排除规则；
- 复用 Chunker 对 table/image/figure 原子 Block 的处理；
- 复用 page/bbox/source block 的追踪语义。

#### 行为边界

- 不修改 PaddleOCR 的阅读顺序模型；
- 不承诺仅靠 bbox 彻底修复复杂多栏顺序；
- 不把 caption 当标题层级；
- 不对正文做模糊去重；
- 被过滤内容不进入 Chunk，但 profile 中保留计数，不记录完整 OCR 文本。

#### 影响模块/函数

- `paddleocr_pdf_adapter.py::_result_to_rows()`
- `PaddleOCRPDFParserAdapter.last_parse_stats/last_parse_profile`
- `pdf_heading_hierarchy.py::_candidate_indices()`
- `ParseArtifactService` 原则上不改，只验证过滤后的统一 Block 能正常序列化

#### 验证方式

- 两页相同 header/footer 不进入正文和 Chunk；
- `number` 页码不进入 Chunk；
- `text -> table -> text` 顺序完全保持；
- caption/reference 不被提升为标题；
- profile 包含按 label 的过滤计数但不包含 OCR 原文；
- 多栏和重叠框样本只验证“不丢内容、可追踪”，不把首期目标扩大为完美重排。

### 5.5 步骤五：把 PaddleOCR 纳入现有结构递归路由

#### 当前解决的问题

当前 `ChunkEngine._chunk_semantic()` 使用：

```python
structured_pdf
and compat_source == "pdf"
```

判断结构化 PDF。DeepDoc 的 `compat_source` 为 `pdf`，PaddleOCR 为 `paddleocr`，所以即使 PaddleOCR 已经输出完整结构，也会继续走旧 General 合并逻辑。

#### 处理方式

路由从“Parser 名称判断”调整为“结构能力判断”。建议：

```python
is_structured_pdf = any(
    bool(metadata.get("structured_pdf"))
    and metadata.get("parser_backend") in {"deepdoc", "paddleocr"}
    for block in text_blocks
)
```

兼容现有 DeepDoc metadata：

- `parser_backend` 缺失但 `structured_pdf=true、compat_source=pdf` 时继续识别为 DeepDoc；
- PaddleOCR 明确写入 `parser_backend=paddleocr`；
- `compat_source` 继续用于来源追踪，不再承担能力开关职责。

结构递归触发条件保持：

```text
ChunkStrategy.SEMANTIC
and document_type == general
and OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED == true
and is_structured_pdf
```

#### 复用 DeepDoc 的内容

- 直接复用 `chunk_general_structured_recursive()`；
- 直接复用现有 `OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED`；
- 复用现有 ChunkStrategy.SEMANTIC，不新增枚举；
- 复用旧 `chunk_semantic_ragflow()` 作为开关关闭或非目标文档的回退。

#### 行为边界

- `auto/pdf/deepdoc` 的 ParserFactory 映射不变；
- PaddleOCR 失败不降级 DeepDoc；
- `chunk_method=pdf_manual` 不改；
- `document_type=manual/laws` 不进入结构递归路由；
- 非 PDF 不受影响；
- 开关关闭时仍走旧 General Chunk 实现；
- 本步骤不新增 Workspace 级策略参数，避免与现有开关形成重叠配置。

需要注意：Adapter 输出会比当前更丰富，`manual/laws` 虽不进入结构递归，但会收到更准确的 block_type/level。现有 document profile 必须做回归测试。如果业务要求 `manual/laws` 的 Chunk 与旧 PaddleOCR 逐字节一致，则需要把 Resolver 调用移动到 document type 已知的编排层；这会同时调整 DeepDoc seam，不属于首选方案，应在测试发现真实差异后单独决策。

#### 影响模块/函数

- `openrag/src/openrag/chunking/chunk_engine.py::_chunk_semantic()`
- `paddleocr_pdf_adapter.py`：写入结构能力和 backend metadata
- `structure_recursive_chunker.py` 原则上无代码改动
- `ParserFactory`、`ParserRegistry`、API 上传/重处理入口原则上无改动

#### 验证方式

- `structured_pdf + parser_backend=paddleocr + general` 进入结构递归；
- 现有 DeepDoc structured route 继续进入结构递归；
- `OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED=false` 时 PaddleOCR 回到旧 General 分块；
- `manual/laws` 不调用 `chunk_general_structured_recursive()`；
- 非结构化 PDF 或普通文档仍走原路径；
- `ChunkStrategy` 枚举值和 `ChunkEngine()` 默认值不变。

### 5.6 步骤六：复用结构递归 Chunker 完成最终切块

#### 当前解决的问题

即使 PaddleOCR 已识别标题层级，如果继续使用旧 General 合并逻辑，Chunk 仍可能跨越一级章节，也无法保证长正文按自然分隔符递归降级。

#### 处理方式

直接复用现有 `chunk_general_structured_recursive()`：

1. `_build_regions()` 只使用 `level=1 + hard_boundary=true` 划分 H1 区域；
2. 第一个 H1 前的文档标题、摘要等形成 preamble；
3. 没有可信 H1 时创建 `document-root` 合成区域；
4. `_block_fragments()` 保持 table/image/figure 原子性；
5. 普通超长文本使用已有递归分隔符链；
6. H2-H6 作为软切点，不是硬边界；
7. `_pack_fragments()`、短尾合并和 overlap 都限制在当前 H1 区域；
8. `_make_chunk()` 输出标题路径、页码、bbox、来源 Block 和置信度元数据。

#### 复用 DeepDoc 的内容

该步骤原则上完全复用：

- H1 区域构建；
- 无 H1 全文兜底；
- 递归文本拆分；
- H2-H6 软切点；
- table/image/figure 原子处理；
- 区域内 overlap；
- 短尾安全合并；
- Chunk 来源和结构 metadata。

PaddleOCR 不新增专属 Chunker，也不在 Chunker 中出现 `ocr_label` 判断。

#### 行为边界

- Chunker 只相信 Resolver 输出的 `level/hard_boundary`；
- `paragraph_title`、Markdown `#` 和 layout score 不在 Chunker 中再次解释；
- 原子表格若自身超过 `chunk_size`，允许形成有原因标记的超限 Chunk，不按正文标点破坏表格；
- overlap 不跨 H1，也不跨原子 Block 或标题；
- 没有 H1 不等于失败，必须使用全文递归兜底；
- OCR 文本错误原样进入 Chunk，本步骤不做文字校正。

#### 影响模块/函数

- `openrag/src/openrag/chunking/structure_recursive_chunker.py`
  - 预计不改实现，只补 PaddleOCR 集成测试
  - 若测试暴露通用缺陷，只允许做后端无关修复
- `openrag/src/openrag/chunking/chunk_engine.py::_chunk_semantic()`
- Chunk metadata 持久化链路原则上不改

#### 验证方式

- 两个可信 H1 产生两个不可跨越的区域；
- Chunk 和 overlap 的 `cross_h1_count` 都为 0；
- H2 短节可在同一 H1 中合并；
- 长正文按段落、换行、句子、分号、逗号、空格和字符兜底；
- 无 H1 文档得到 `document-root` 区域并成功分块；
- table 保持原子性和原始位置；
- Chunk 保留 `heading_path/page/bbox/source_block_id/source_char_start/source_char_end`。

## 6. 预期代码改动清单

| 模块/函数 | 改动性质 | 解决的问题 |
|---|---|---|
| `parsers/adapters/paddleocr_pdf_adapter.py::parse` | 修改 | 在远端结果转换后执行结构证据准备和标题 Resolver |
| `paddleocr_pdf_adapter.py::_result_to_rows` | 修改 | 保留 label、统一类型、过滤噪声、写入结构 metadata |
| Paddle Adapter 私有 layout matcher | 新增 | 将 `layout_det_res.score/order` 关联到 parsing Block |
| Paddle Adapter 私有 Markdown matcher | 新增 | 将 Markdown 相对标题深度关联到候选 Block |
| `parsers/pdf_heading_hierarchy.py::extract_pdf_outlines` | 新增或集中 | 从原始 PDF 获取 Outline，避免依赖 DeepDoc 内部对象 |
| `PdfHeadingHierarchyResolver.resolve` | 扩展 | 接受 Paddle evidence 并保护 `doc_title` |
| `PdfHeadingHierarchyResolver._candidate_indices` | 扩展 | 识别 Paddle 标题候选和噪声类型 |
| `_style_value/_same_style_source` | 扩展 | 支持 bbox 归一化几何样式，不伪造字号 |
| `_match_outlines/_parse_numbering/_assign_heading_paths` | 复用/回归 | 保持 DeepDoc 与 Paddle 共用层级算法 |
| `chunking/chunk_engine.py::_chunk_semantic` | 小改 | 从来源判断改为结构能力判断 |
| `chunking/structure_recursive_chunker.py` | 原则上不改 | 直接复用现有结构递归算法 |
| `services/parse_artifact_service.py` | 原则上不改 | 验证新增 metadata 能正常序列化和重放 |
| `processors/document_processor.py` | 原则上不改 | 保持编排稳定，仅验证 trace/profile |
| `parsers/factory.py` | 不改 | 保持 `auto/pdf=PaddleOCR、deepdoc=DeepDoc` 的公开选择 |

## 7. 测试与验证方案

### 7.1 固化实测 Fixture

从 33 节点实测响应中生成脱敏、最小化 JSON Fixture，只保留：

```text
pages.page_index/width/height
parsing_res_list.label/bbox/content/polygon_points
layout_det_res.boxes.label/coordinate/score/order
markdown.markdown_texts
doc_preprocessor_res.angle
```

删除：

- 临时服务器路径；
- image_ref；
- 与结构转换无关的大字段；
- 任何真实业务文档内容。

建议路径：

```text
openrag/tests/fixtures/paddleocr/structured_two_page.json
```

Fixture 预期结构：文档标题、两个 H1、四个 H2、正文、表格、两个页码。

### 7.2 Adapter 单元测试

扩展 `openrag/tests/test_paddleocr_pdf_adapter.py`：

- label 到统一类型映射；
- `doc_title` 角色保护；
- `paragraph_title` 只成为候选；
- table HTML、顺序和 bbox 保留；
- number/header/footer 过滤；
- Layout exact/IoU/一对多/未命中关联；
- Markdown 标题匹配和相对层级规范化；
- bbox 缩放、旋转、裁剪和无效坐标回归；
- score 缺失不阻塞解析；
- profile 记录过滤数量，不记录 OCR 原文；
- 远端失败、超时和错误脱敏行为不变。

### 7.3 Resolver 契约测试

扩展 `openrag/tests/test_pdf_heading_hierarchy.py`：

- Paddle `doc_title` 永不成为 H1；
- Paddle `paragraph_title + Outline` 得到 H1/H2；
- Paddle `paragraph_title + 第一章/1.1` 得到 H1/H2；
- Paddle Markdown 与编号一致时增强置信度；
- Markdown 单证据不产生硬 H1；
- geometry-only 不产生硬 H1；
- Outline/编号/Markdown 冲突时降级并记录 sources/conflict；
- 无可信层级时回到 text/level=0；
- parent id 和 heading path 正确传播；
- 所有现有 DeepDoc Resolver 测试继续通过。

### 7.4 Chunk 路由与算法测试

扩展：

- `openrag/tests/test_structure_recursive_chunker.py`
- `openrag/tests/test_paddleocr_pdf_integration.py`

覆盖：

- Paddle structured general 进入结构递归；
- DeepDoc structured general 仍进入结构递归；
- feature flag 关闭回退旧 General；
- manual/laws 不进入结构递归；
- H1、overlap 和短尾合并不跨区域；
- 无 H1 全文兜底；
- table 原子性；
- PDF page/bbox/source span 在 Chunk 中保持；
- 公开 `pdf/deepdoc/auto` 路由和旧 `paddleocr` 拒绝行为不变。

### 7.5 Parse Artifact 与回源验证

通过 `DocumentProcessor.process_document()` 的集成测试验证：

- canonical JSON/Markdown 保留 heading、table、page、bbox 和 metadata；
- 过滤的页码不进入 canonical content；
- Chunk 能追溯到来源 Block；
- PDF 点击回源仍使用正确 page/bbox；
- 不新增数据库字段和迁移；
- 新 metadata 能被 JSON 序列化；
- reprocess 后可以用同一份解析结果稳定产生 Chunk。

### 7.6 金标 PDF 集合

至少覆盖：

1. 数字 PDF，有 Outline、有文档标题、H1/H2/H3；
2. 数字 PDF，无 Outline，但编号规范；
3. 扫描 PDF，编号规范；
4. 扫描 PDF，无编号，仅有明显几何样式；
5. 无可靠 H1 的连续正文；
6. 两栏或复杂阅读顺序；
7. 表格位于两个正文 Block 中间；
8. 重复页眉页脚和页码；
9. 目录页包含大量类似标题的条目；
10. 文档标题与第一个章节标题同页；
11. 同页出现相同标题文本；
12. 超长正文和超长表格。

人工标注：

```text
block text/type/page/bbox/order
document_title
heading_candidate
heading_level
parent_heading
hard_boundary
noise
```

### 7.7 指标

#### 解析和标题指标

- Block 文本覆盖率、重复率和顺序准确率；
- page/bbox 覆盖率；
- table 相对位置准确率；
- 噪声过滤 Precision/Recall；
- 标题候选 Precision/Recall；
- H1 Precision/Recall，优先保证 H1 Precision；
- H1/H2/H3 Macro-F1；
- parent relation accuracy；
- 文档标题误判 H1 比例；
- Layout/Markdown/Outline 匹配率和冲突率。

#### Chunk 指标

- `cross_h1_chunk_count == 0`；
- `cross_h1_overlap_count == 0`；
- Chunk Token 长度分布；
- 超限 Chunk 数及其是否仅来自原子 Block；
- 原文字符覆盖率、重复率和乱序率；
- Chunk 到 heading/page/bbox/source block 的可追踪率；
- 无 H1 文档成功分块率。

#### RAG 和运行指标

- Recall@K、Hit@K、MRR、NDCG；
- 答案准确率；
- 引用页码和 bbox 正确率；
- PaddleOCR 解析耗时、标题 Resolver 耗时和入库总时延；
- 数字 PDF 与扫描 PDF 分组表现；
- 新旧 Chunk 数量和平均长度变化。

## 8. 推荐实施顺序

每一步独立提交、独立验证：

1. 固化 PaddleOCR 脱敏 Fixture 和当前 Adapter 基线断言；
2. 保留 label、统一 Block 类型、过滤明确噪声；
3. 实现 Layout evidence 关联，不接标题 Resolver；
4. 实现 Markdown heading 解析和匹配，不设置硬边界；
5. 增加 Paddle Outline 提取；
6. 扩展通用 Resolver 的 Paddle 候选、文档标题保护和几何样式；
7. 在纯 `DocumentBlock` 测试中验证证据组合和 H1 门控；
8. Paddle Adapter 调用 Resolver，验证 parse artifact；
9. 扩展 `ChunkEngine` 的结构能力路由；
10. 复用现有 Chunker 完成端到端测试；
11. 使用金标 PDF 对比旧 Paddle Chunk、新 Paddle 结构 Chunk和 DeepDoc 结构 Chunk；
12. 在测试 Workspace 重处理并重建索引，验证 RAG 指标后再扩大范围。

不应把 Adapter 映射、证据关联、Resolver 扩展和生产路由放在一个不可拆分提交中。

## 9. 上线、观察与回滚

推荐分四阶段。前两个阶段显式设置：

```text
OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED=false
```

此时 Paddle Adapter 和 Resolver 可以生成结构 metadata，但 ChunkEngine 仍走旧 General 分块，因此不需要增加第二个 shadow 开关。

1. **结构 Shadow**：生成新 Block metadata 和匹配指标，但不启用 Paddle H1 硬边界；
2. **层级 Shadow**：运行 Resolver，记录 level/source/conflict，与人工金标比较；
3. **33 测试环境启用**：在 33 节点设置开关为 `true`，重处理指定测试 Workspace 并重建 Chunk 和向量；
4. **生产启用**：指标通过后再在生产环境开启，保留旧 General 分块一个发布周期。

当前开关是进程级而不是 Workspace 级。它不能在同一个 Worker 进程内只对一个生产 Workspace 生效。如果生产必须进行 Workspace 级 canary，需要先增加 Workspace 配置或部署独立 canary Worker；这属于额外发布能力，不在本方案中假定已经存在。没有 canary 能力时，应先在 33 测试环境完成主动验证，再安排生产开关切换窗口。

回滚方式：

```text
OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED=false
```

关闭后 General 回到 `chunk_semantic_ragflow()`。但已经按新策略生成并写入索引的 Chunk 不会自动恢复，必须：

1. 基于 parse artifact 重新分块，或重新解析；
2. 重建对应文档的 Embedding 和向量索引；
3. 验证旧 Chunk 已被正确替换。

如果回滚目标还要求恢复 PaddleOCR 当前“除表格外全部 text/level=0”的解析 artifact，则仅关闭 Chunk 开关不够，需要回滚 Adapter 适配提交并重新解析。方案实施时应保持 Adapter、Resolver 和 Chunk 路由提交可分别回退。

## 10. 明确不做的事情

- 不修改或重新训练 PaddleOCR 模型；
- 不替换现有业务 OCR 校正；
- 不修改远端 PaddleOCR 异步接口；
- 不引入 Docling；
- 不新增公开 `ChunkStrategy` 枚举；
- 不新增 PaddleOCR 专属 Chunker；
- 不新增 PaddleOCR 专属标题 Resolver；
- 不把所有 `paragraph_title` 当作 H1；
- 不把文档标题当作 H1；
- 不用 LLM 作为首期标题层级判断器；
- 不伪造字体大小、粗体、Outline、层级或 bbox；
- 不在首期彻底解决所有多栏阅读顺序和重叠 OCR 框；
- 不改变 `auto/pdf/deepdoc` 的公开 Parser 选择；
- 不让 PaddleOCR 失败自动降级 DeepDoc；
- 不自动重建全部历史 PDF 索引；
- 不新增数据库字段；
- 不扩展到非 PDF 文档。

## 11. 最终验收标准

代码完成后必须同时满足：

1. PaddleOCR Adapter 保留 `doc_title/paragraph_title/text/table/image` 等结构语义；
2. 页码、页眉、页脚不进入 canonical content 和 Chunk；
3. Layout score 和 Markdown 标题深度可以追溯到对应 Block；
4. PDF Outline 可在 Paddle 链路中参与层级推断；
5. 文档标题永远不成为硬 H1；
6. `paragraph_title` 单证据永远不成为硬 H1；
7. 只有可信 `level=1 + hard_boundary=true` 才切分 H1 区域；
8. Chunk、overlap、短尾合并都不跨可信 H1；
9. 无可信 H1 时全文递归分块成功；
10. 表格保持原子性、顺序、page 和 bbox；
11. `auto/pdf` 继续选择 PaddleOCR，`deepdoc` 继续选择 DeepDoc；
12. `manual/laws` 和非 PDF 不进入结构递归路由；
13. 关闭现有开关可以回退旧 General Chunk；
14. DeepDoc 现有结构化 Adapter、Resolver 和 Chunker 测试不回归；
15. PaddleOCR 远端错误、超时、日志脱敏和 bbox 缩放行为不回归；
16. 金标集合上 H1 Precision、跨 H1 Chunk 数、回源正确率和 RAG 指标达到上线门槛。

## 12. 结论

PaddleOCR 适配的关键不是修改 OCR 模型，也不是再开发一种分块算法，而是把现有模型已经返回的结构信息完整、可信地接到 OpenRAG 已有的结构 seam：

```text
PaddleOCR label/content/bbox/layout/markdown
  -> 统一 DocumentBlock
  -> 共用 PdfHeadingHierarchyResolver
  -> 共用 GeneralStructuredRecursiveChunker
```

真正新增的 Paddle 专属实现只应包括：

1. label 和噪声归一化；
2. `layout_det_res` 与 `parsing_res_list` 的版面证据关联；
3. Paddle Markdown 标题与 Block 的匹配；
4. 原始 PDF Outline 的读取入口。

标题编号、Outline 对齐、置信度语义、标题路径、H1 门控、递归分隔符、原子表格、overlap 和无 H1 兜底均复用 DeepDoc 已实现的通用模块。这样可以把生产默认 PaddleOCR 链路纳入新策略，同时保持 Parser 选择、OCR 资产、公开枚举、非 General 模式和非 PDF 行为稳定。
