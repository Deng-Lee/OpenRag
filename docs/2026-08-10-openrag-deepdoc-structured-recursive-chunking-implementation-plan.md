# OpenRAG 基于 DeepDoc 的结构感知递归切块执行方案

> 日期：2026-08-10
> 目标分支：`fix-rank-bug`
> 适用范围：PDF 文件、`general` 文档类型、现有 `ChunkStrategy.SEMANTIC` 内部实现
> 核心约束：继续使用 DeepDoc 和现有业务 OCR；不引入 Docling Parser；不修改公开的 `ChunkStrategy` 枚举和 `ChunkEngine()` 默认策略。

## 1. 目标与成功标准

本方案要解决四个连续问题：

1. 当前 PDF Adapter 消费 DeepDoc 的扁平 `text_body + tbls` 结果，把标题、正文等重新构造成普通 `text / level=0`，导致版面结构丢失。
2. OpenRAG 没有稳定识别、保存并利用 PDF 标题层级，无法区分文档标题、H1、H2、H3 和普通正文。
3. 当前 `general` 分块只以已有 `DocumentBlock` 为拼装单元，没有把可信 H1 作为不可跨越的边界，也没有实现完整的递归分隔符降级链。
4. 当 PDF 中没有可靠 H1 时，需要保证仍可基于全文递归分块，而不是错误制造 H1 或退回简单固定长度切块。

完成后的核心行为是：

```text
PDF
  -> DeepDoc OCR / 版面识别 / 表格识别
  -> 有序结构化 block 流（title/text/table/figure）
  -> 标题候选识别
  -> 全文级标题层级推断（H1/H2/H3）
  -> 可信 H1 划分硬边界区域
  -> 每个 H1 区域内按 Block 组织并递归分块
  -> 没有可信 H1 时，以全文作为一个合成根区域递归分块
  -> Chunk 保留标题路径、页码、bbox 和来源 Block
```

验收时必须满足：

- 表格、图片与正文的顺序不再因 Adapter 分流而统一移动到文末；
- DeepDoc 的 `title/text/table/figure` 类型能够进入 `DocumentBlock`；
- 只有经过可信证据确认的 H1 才能成为硬边界；
- 任意 Chunk 和 overlap 都不得跨越可信 H1；
- 没有可信 H1 的文档仍能完成全文递归分块；
- 除显式处理的超长表格等特殊情况外，Chunk 的实际 Token 数不超过 `chunk_size`；
- 现有非 PDF、非 `general` 模式及公开 `ChunkStrategy` 行为不受影响；
- 新链路可以通过开关回退到当前实现。

## 2. 当前问题定位

### 2.1 Adapter 使用了扁平接口

当前 `PDFParserAdapter._parse_ragflow_with_tables()` 调用：

```python
text_body, tbls = self.ragflow_parser(file_path)
```

对应文件：

- `openrag/src/openrag/parsers/adapters/pdf_adapter.py`
- `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py`

`RAGFlowPdfParser.__call__()` 最终返回：

```python
tuple[str, list]
```

其中正文已经由 `__filterout_scraps()` 压成带位置标签的字符串；表格和图片被放入另一个列表。正文原始 Box 上的 `layout_type`、`layoutno` 等字段不再随字符串返回。

Adapter 随后按 `\n\n` 拆分 `text_body`，并统一构造：

```python
block_type = "text"
layout_type = "text"
level = 0
```

这会把 DeepDoc 已识别的 `title` 再次降为普通正文。Adapter 又在处理完全部正文后统一追加 `tbls`，因此可能把原本位于两段正文之间的表格或图片移到文末；同时 `tbls` 中的图片也可能被统一当作表格处理。

### 2.2 DeepDoc 已有结构化结果，但主链路没有消费

`RAGFlowPdfParser.parse_into_bboxes()` 返回 `deepcopy(self.boxes)`，其元素保留：

- `text`；
- `layout_type`，例如 `title/text/table/figure`；
- `page_number`；
- `x0/x1/top/bottom`；
- `layoutno`；
- `positions`；
- 表格或图片数据。

该方法还会在返回前把表格和图片插回 `self.boxes`，形成统一的有序 Block 流。插入位置由 bbox 距离启发式计算，虽然仍需用复杂版面样本验证，但它比 Adapter 先处理全文、再统一追加表格更接近原始阅读顺序。

需要明确：`parse_into_bboxes()` 只保留了“这是标题候选”的粗粒度信息，不会直接稳定输出 H1/H2/H3。调用结构化接口只是修复信息丢失的第一步，不能替代标题层级推断。

### 2.3 标题层级没有形成可信语义

`DocumentBlock` 已经具有 `block_type`、`layout_type`、`level`、`page`、`bbox` 和 `metadata` 等字段，字段容量基本够用。真正缺少的是一条把解析证据转成标题语义的链路：

```text
DeepDoc title 候选
  + PDF Outline
  + 标题编号体系
  + 字号/行高/粗体等样式
  + 版面位置和全文一致性
  -> heading level + confidence + parent + heading path
```

目前正文 `level` 被 Adapter 固定为 0，因此下游无法区分普通正文和多级标题。

### 2.4 当前 Chunker 没有强制 H1 边界

`ChunkEngine()` 默认使用 `ChunkStrategy.SEMANTIC`，现有测试和调用方已经依赖这一名称和默认值。当前 `general` 语义分块主要按 Token 阈值拼装已有 Block：加入后不超限则继续合并，超限则开启新 Chunk。

该过程没有保证：

- H1 前必须切分；
- overlap 不跨 H1；
- H2/H3 作为优先切点；
- 超长正文按照段落、句子、标点和空格逐级递归拆分；
- 无 H1 文档使用相同递归算法兜底。

## 3. 设计原则与行为边界

### 3.1 保持 DeepDoc 和现有 OCR

- DeepDoc 继续负责 PDF 文本获取、OCR、版面识别、表格识别和基础阅读顺序。
- 现有针对业务校正过的 OCR 不替换、不绕过。
- Docling、PaddleX、PyMuPDF4LLM 等仅作为标题层级推断算法的参考，不作为运行时依赖。
- OCR 负责提供文字和细粒度坐标；版面模型负责提供 `title/text/table/figure` 等粗分类；两者都不单独决定 H1/H2/H3。

### 3.2 H1 高精度优先

错误 H1 会制造错误硬边界，比漏掉一个软标题危害更大。因此：

- Outline 高置信匹配和全文一致的强编号可以形成硬证据；
- 字号、行高或 DeepDoc `title` 单独命中只能形成弱证据；
- 弱证据可以保留为标题候选或软标题，但不能直接成为 H1 硬边界；
- 文档封面标题必须与章节 H1 分开，文档标题不成为硬边界；
- 强证据冲突时降级，不强行选择 H1。

### 3.3 保持兼容，缩小改动范围

- 不新增或重命名公开 `ChunkStrategy` 枚举值；
- 不修改 `ChunkEngine()` 默认的 `SEMANTIC`；
- 只在 `document_type == "general"` 且启用结构递归开关时进入新实现；
- `manual/laws` 等现有 profile 分块保持不变；
- 第一阶段仅覆盖 PDF，其他解析器仍走原逻辑；
- 旧 PDF Adapter 和旧 General 语义分块保留一个发布周期作为回退路径。

## 4. 目标数据语义

第一阶段复用现有 `DocumentBlock`，避免为单一目标引入新的公共文档 AST。新增信息优先写入 `metadata`。

### 4.1 确认标题后的 DocumentBlock

```python
DocumentBlock(
    block_type="heading",
    layout_type="title",
    level=1,                     # 1..6；正文仍为 0
    text="第一章 系统概述",
    page=3,
    bbox=(...),
    metadata={
        "heading_role": "section",       # section/document_title
        "heading_confidence": "hard",    # hard/supported/soft/conflict
        "heading_score": 0.96,
        "heading_sources": ["outline", "numbering"],
        "parent_heading_id": None,
        "heading_path": ["第一章 系统概述"],
        "hard_boundary": True,
        "source_block_ids": ["..."],
    },
)
```

约束：

- `level=0` 永远表示“没有确认标题层级”，不得由下游自动解释为 H1；
- `block_type="heading"` 只用于已经完成层级解析的标题；
- DeepDoc `layout_type="title"` 但证据不足的 Block 保持 `block_type="text"`、`level=0`，并在 metadata 标记 `heading_candidate=true`；
- `hard_boundary=true` 只允许出现在 `heading_role=section`、`level=1` 且置信度为 `hard/supported` 的 Block 上；
- `heading_role=document_title` 永远不是硬边界。

### 4.2 正文、表格和图片继承结构路径

标题树建立后，后续正文、表格和图片写入：

```python
metadata={
    "active_heading_ids": ["h1-id", "h2-id"],
    "heading_path": ["第一章 系统概述", "1.2 部署方式"],
}
```

如果一个 Chunk 包含多个 H2/H3，Chunk metadata 不伪造单一末级标题，而是同时保存：

```python
{
    "h1_region_id": "h1-id",
    "h1_title": "第一章 系统概述",
    "active_heading_path": [...],
    "contained_heading_ids": [...],
    "contained_heading_paths": [...],
    "source_block_ids": [...],
    "page_range": [3, 5],
    "hard_boundary_respected": True,
}
```

## 5. 分阶段实施方案

### 阶段一：让 PDF Adapter 消费有序结构化 Block

#### 解决的问题

修复标题类型、版面类型、表格/图片相对顺序在 Adapter 中丢失的问题。

#### 实现方式

将当前：

```python
text_body, tbls = parser(file_path)

for paragraph in text_body.split("\n\n"):
    append(text_block)

for table in tbls:
    append(table_block)
```

调整为：

```python
ordered_boxes = parser.parse_into_bboxes(file_path)

for order, box in enumerate(ordered_boxes):
    blocks.append(convert_deepdoc_box(box, order=order))
```

新增一个私有、纯转换函数或模块，例如：

```python
def convert_deepdoc_box(box: dict, *, order: int) -> DocumentBlock:
    ...
```

这个接口后面隐藏以下实现细节：

- `page_number -> page`；
- `x0/x1/top/bottom -> bbox`；
- `title/text/table/figure -> block_type/layout_type`；
- `positions -> metadata.line_positions/source_spans`；
- 图片二进制和表格文本/HTML映射；
- `layoutno`、原始顺序、DeepDoc 证据写入 metadata；
- 稳定生成 `block_id` 和字符范围。

这里形成一个清晰 seam：Adapter 只知道“将有序 DeepDoc Box 转成 `DocumentBlock`”，不负责标题层级判断；标题 Resolver 也不需要理解 DeepDoc 的原始字典结构。

#### 行为边界

- 本阶段只保留 DeepDoc 已输出的粗结构，不推断 H1/H2/H3；
- 不修改 OCR 模型和调用方式；
- 不声称 `parse_into_bboxes()` 的 bbox 最近距离插入能解决所有多栏、浮动图表和跨页表格顺序问题；这些作为金标样本验证项；
- 对 `parse_into_bboxes()` 与 `__call__()` 在 `_naive_vertical_merge`、过滤、Profile 记录等方面的差异建立回归测试，不做无验证的直接替换；
- 若结构化路径异常，可受控回退当前 `__call__()` 路径，并标记 `structure_quality=degraded`。

#### 影响模块

- `openrag/src/openrag/parsers/adapters/pdf_adapter.py`
- `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py`（仅在需要补充 Profile 或暴露证据时修改）
- `openrag/src/openrag/parsers/base.py`（原则上不新增字段，仅校正文档语义）
- PDF Adapter 相关单元测试和集成测试

### 阶段二：保留标题层级推断所需证据

#### 解决的问题

`parse_into_bboxes()` 虽保留 `title` 和坐标，但仅靠 `title` 无法稳定判断 H1/H2/H3；当前部分字体和版面置信信息会在 DeepDoc 中间流程丢失。

#### 实现方式

在不改变 OCR 结果的前提下，补充并向 Box/metadata 传递以下证据：

1. **版面证据**
   - `layout_type`；
   - 版面模型检测分数 `layout_score`；
   - `layoutno`；
   - page/bbox、宽高、居中程度、左右缩进、上下留白。

2. **数字 PDF 字体证据**
   - 从 `pdfplumber` 字符集合中，在删除原始 chars 之前聚合：
     - `font_size_median`；
     - `font_name_mode`；
     - `bold_ratio`；
     - `char_height_median`；
     - `char_count`；
     - `style_source=pdf_chars`。

3. **扫描 PDF 几何证据**
   - OCR 行框高度；
   - Block 高度和行数；
   - 与同页正文中位行高的比例；
   - `style_source=ocr_geometry`。

扫描页没有真实字体信息，不伪造 `font_size`。

4. **PDF Outline**
   - 提取 `title`；
   - Outline 树深度；
   - 目标页码；
   - 可用时保留目标位置。

5. **解析来源**
   - 原生 PDF 文本层或 OCR；
   - parser/model 版本；
   - 证据缺失原因。

#### 版面置信度语义

`layout_score` 是版面检测模型对 `title/text/table/...` 区域的原始检测得分，不是：

- OCR 文字置信度；
- H1/H2/H3 层级置信度；
- 已校准概率。

它只能作为“是否像标题”的一个输入，不能单独决定标题层级或硬边界。

#### 影响模块

- `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py`
- `openrag/src/openrag/parsers/ragflow/vision/layout_recognizer.py` 或当前实际 LayoutRecognizer 实现
- PDF Adapter 的 Box 转换函数

### 阶段三：识别标题候选

#### 解决的问题

在全部正文 Block 中先保守筛出“可能是标题”的少量候选，避免对每一段正文直接运行复杂层级规则，也避免把列表项、页眉页脚和文档标题误当成章节 H1。

#### 实现方式

新增内部模块：

```text
openrag/src/openrag/parsers/pdf_heading_hierarchy.py
```

对外只提供一个较小接口：

```python
class PdfHeadingHierarchyResolver:
    def resolve(
        self,
        blocks: list[DocumentBlock],
        outlines: list[PdfOutlineEntry],
    ) -> list[DocumentBlock]:
        ...
```

候选识别、Outline 匹配、编号分析、样式归组和冲突处理都放在该模块内部，避免规则散落到 Adapter、Sectionizer 和 Chunker。

候选来源按优先级包括：

1. DeepDoc `layout_type=title`；
2. 同页、同 `layoutno`、版式一致的连续多行标题合并成一个虚拟候选；
3. 与 Outline 标题在目标页高相似度匹配的短独立 Block；
4. 命中强章节编号且具备标题形态的短 Block，例如“第一章”“第2节”“1.2 系统架构”“Chapter 3”。

候选排除规则包括：

- table/figure/caption/reference；
- 重复出现的页眉页脚；
- 独立页码；
- 明显完整的长正文句；
- 连续列表项；
- 目录页中的大量条目；
- 封面文档标题，单独标记 `role_hint=document_title`。

候选识别失败不会删除内容。被排除或最终未确认的 Block 继续作为普通正文进入分块。

建议 MVP 先启用：

- DeepDoc `title`；
- Outline 对已有 `title` 的匹配；
- 明确的“章/篇/Part/Chapter”等强编号。

任意普通 `text` 仅凭字号大就提升成标题的策略先关闭，等金标验证后再灰度开启。

### 阶段四：推断标题层级并形成可信 H1

#### 解决的问题

将“标题候选”转换为可供结构树和 Chunker 使用的 H1/H2/H3，同时避免把 DeepDoc 的所有 `title` 默认当成 H1。

#### 4.1 Outline 层级与页面 Block 对齐

PDF Outline 本身通常直接提供嵌套树结构，因此 Outline 深度可以映射为候选层级：

```text
depth=0 -> H1
depth=1 -> H2
depth=2 -> H3
```

但 Outline 是导航元数据，不是正文阅读流中的 `DocumentBlock`。Chunker 必须知道标题在正文 Block 序列中的准确位置，所以需要将 Outline 条目匹配回页面 Block。

匹配键为：

```text
目标页码 + 归一化文本 + 去编号文本 + 可用时的 Y 坐标
```

匹配要求：

- 同页优先；
- 标题文本做空白、标点、全半角和编号前缀归一化；
- 同一 Block 只能匹配一个 Outline 条目；
- 多个相似候选时使用 Y 距离和文本相似度联合排序；
- 未命中的 Outline 只记录，不制造不存在的页面 Block；
- 未匹配 Outline 的标题候选继续回退到编号和样式证据。

#### 4.2 全文编号体系分析

不能逐行独立套所有正则，而应先把每个候选解析为编号 Token：

```python
NumberingToken(
    raw_prefix="1.2.3",
    family="arabic_dotted",
    value=(1, 2, 3),
    intrinsic_depth=3,
    title_text="部署参数",
)
```

首期支持的编号家族：

- `第一篇/第一章/第一节/第一条`；
- `一、/（一）/1）/（1）`；
- `1/1.1/1.1.1`；
- 罗马数字和英文字母编号；
- `Part/Chapter/Section/Article/Appendix`。

然后在全文范围评估：

- 同一编号家族的覆盖率；
- 序号是否大体连续；
- 子编号前缀是否存在父编号；
- 子编号是否在父章节切换时合理重置；
- 编号深度是否与 Outline 对齐；
- 同级编号的样式是否一致；
- 是否更像连续列表而不是标题。

点分编号的深度可以直接提供强提示，但不能在没有任何锚点时把全文只出现的 `1.1/1.2` 自动压缩成 H1。由于 H1 是硬边界，缺少父级锚点时宁可降级成软标题。

#### 4.3 字体和版面样式推断

样式只能在全文候选集合中相对比较：

- 数字 PDF 优先比较字体中位数、粗体比例、字体族、缩进和上下留白；
- 扫描 PDF 比较 OCR 行高、Block 高度和相对正文行高；
- 将样式相近的候选归组；
- 由已通过 Outline 或强编号确认的标题给样式组提供层级锚点；
- 没有锚点的样式组只能产生低置信软标题，不直接产生硬 H1。

#### 4.4 证据组合与冲突降级

证据优先级：

```text
Outline > 强编号 > 有锚点的样式组 > 无锚点样式 > DeepDoc title
```

建议使用规则化决策而非首期训练新模型：

```python
if outline_match.is_high_confidence:
    level = outline_match.level
    confidence = "hard"
elif numbering.is_strong_and_consistent:
    level = numbering.level
    confidence = "hard"
elif numbering.agrees_with(anchored_style):
    level = numbering.level
    confidence = "supported"
elif anchored_style.is_reliable:
    level = anchored_style.level
    confidence = "supported"
elif layout_type == "title":
    level = None
    confidence = "soft"
else:
    level = None
```

强证据冲突时：

```python
level = None
confidence = "conflict"
hard_boundary = False
```

最终硬边界条件：

```python
hard_boundary = (
    heading_role == "section"
    and level == 1
    and confidence in {"hard", "supported"}
)
```

### 阶段五：建立标题树和 H1 区域

#### 解决的问题

把孤立的 `level` 转成父子结构和标题路径，并修复当前 Sectionizer 可能把 `block_type=title/heading` 且 `level=0` 当成一级标题的问题。

#### 实现方式

修改 `document_sectionizer.py` 的标题判定：

```python
def _is_parser_heading(item):
    return item.level is not None and item.level > 0
```

不能再使用“`block_type` 是 title/heading 就至少当成 level 1”的兜底逻辑。

使用标题栈构建父子关系：

```python
stack = []

for block in blocks:
    if block.level > 0:
        while stack and stack[-1].level >= block.level:
            stack.pop()
        block.parent_heading_id = stack[-1].id if stack else None
        block.heading_path = [x.text for x in stack] + [block.text]
        stack.append(block)
    else:
        block.heading_path = [x.text for x in stack]
```

随后按可信 H1 划分区域：

```text
首个 H1 前的内容 -> preamble region
H1-A 到下一个可信 H1 前 -> H1-A region
H1-B 到下一个可信 H1 前 -> H1-B region
```

规则：

- H1 是硬边界；
- H2-H6 是软边界；
- 页码不是硬边界；
- 文档标题进入 preamble 或文档 metadata，不创建空 H1 区域；
- 一个 H1 区域允许跨页；
- H1 标题尽量与该区域首段正文粘连，避免形成只有标题的悬空 Chunk。

#### 影响模块

- `openrag/src/openrag/chunking/document_sectionizer.py`
- 标题 Resolver
- Sectionizer 单元测试

### 阶段六：实现 H1 区域内递归分块

#### 解决的问题

在不跨可信 H1 的前提下，允许同一 H1 下的多个短子节合并；当内容超长时按照自然结构逐级拆分。

#### 模块 seam

新增内部模块：

```text
openrag/src/openrag/chunking/structure_recursive_chunker.py
```

对外保持一个接口：

```python
def chunk_general_structured_recursive(
    blocks: list[DocumentBlock],
    *,
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_tokens: int,
) -> list[Chunk]:
    ...
```

模块内部负责：

- H1 区域划分；
- Block 到 Fragment 的转换；
- 超长正文递归拆分；
- H2/H3 优先切点；
- Token 预算装箱；
- 区域内 overlap；
- 表格和图片特殊处理；
- Chunk 来源和结构 metadata 汇总。

#### 6.1 先以 DocumentBlock 为基本组织单元

在同一 H1 区域中按原始顺序加入 Block：

```python
if tokens(current + next_block) <= chunk_size:
    current.append(next_block)
else:
    flush(current)
    current = [next_block]
```

H2/H3 不要求立即 flush，但在接近上限、存在多个合法切点时，优先在 H2/H3 前切分。

#### 6.2 超长正文递归拆分

只有单个文本 Block 自身超过上限时，才进入递归分隔符链：

```text
段落/空行（\n\n）
  -> 换行（\n）
  -> 句末标点（。！？.!?）
  -> 分号（；;）
  -> 逗号（，,）
  -> 空白
  -> 原文字符前缀兜底
```

伪代码：

```python
def recursive_split(text, separators, limit):
    if token_count(text) <= limit:
        return [text]

    if not separators:
        return split_longest_original_prefix(text, limit)

    pieces = split_keep_separator(text, separators[0])
    if len(pieces) == 1:
        return recursive_split(text, separators[1:], limit)

    result = []
    buffer = ""
    for piece in pieces:
        if token_count(buffer + piece) <= limit:
            buffer += piece
        else:
            if buffer:
                result.append(buffer)
            if token_count(piece) <= limit:
                buffer = piece
            else:
                result.extend(recursive_split(piece, separators[1:], limit))
                buffer = ""
    if buffer:
        result.append(buffer)
    return result
```

最终兜底在原始字符串上查找不超过 Token 上限的最长字符前缀，不先 encode 再 decode，避免字符范围和原文定位失真。

#### 6.3 overlap 和短尾合并

- overlap 只从同一 H1 区域内上一个 Chunk 的尾部获取；
- overlap 优先使用完整 Fragment，不跨 H1；
- 不跨表格、图片等原子类型复制 overlap；
- 添加 overlap 后仍必须满足 Token 上限；
- `min_chunk_tokens` 的短尾合并也不得跨 H1；
- 无法安全合并的短尾允许保留为短 Chunk，不以破坏硬边界换取长度均匀。

#### 6.4 表格和图片

- 小表格作为原子 Fragment，不按普通正文标点拆分；
- 超长表格首期明确记录 `over_limit_reason=atomic_table`，或在已有可靠行结构时按行拆分并重复表头；不要把 HTML/单元格文本按逗号递归切碎；
- 图片二进制不参与 Token 计数，caption/description 作为文本 Fragment；
- 表格、图片保持阶段一得到的阅读顺序；
- 所有特殊 Block 保留 page/bbox/source block id。

### 阶段七：无可信 H1 的全文递归兜底

#### 解决的问题

扫描件、无 Outline、无编号或版式不稳定的 PDF 可能无法可靠识别 H1。系统不能因此失败，也不能为了分区而伪造 H1。

#### 实现方式

若全文没有满足硬边界条件的 H1：

```python
regions = [SyntheticRegion(id="document-root", blocks=all_blocks)]
structure_quality = "no_trusted_h1"
```

然后对该合成根区域执行与 H1 区域完全相同的 Block 装箱和递归分隔算法。

此时：

- 已确认的 H2/H3 或软标题仍可以作为优先切点；
- 不存在跨 H1 问题；
- Chunk metadata 明确写入 `fallback_reason=no_trusted_h1`；
- 不切换为公开的 `FIXED_SIZE` 策略；
- 不把所有 DeepDoc `title` 强行提升成 H1。

### 阶段八：接入现有 ChunkEngine 并灰度

#### 解决的问题

在不破坏现有枚举、Worker 默认值和调用方测试的前提下引入新逻辑。

#### 实现方式

在 `ChunkEngine._chunk_semantic()` 内部路由：

```python
if (
    document_type == "general"
    and structured_recursive_enabled
    and source_type == "pdf"
):
    return chunk_general_structured_recursive(...)

return chunk_semantic_ragflow(...)
```

推荐开关：

```text
OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED=false|true
```

如果当前配置体系支持 Workspace 级开关，灰度优先放在 Workspace；否则先使用进程级开关完成 shadow 验证。不要同时新增多个互相重叠的策略参数。

#### 影响模块

- `openrag/src/openrag/chunking/chunk_engine.py`
- 新增 `structure_recursive_chunker.py`
- Worker 调用链原则上无须改名或切换枚举
- Chunk metadata 序列化和索引写入链路

## 6. 预期代码改动清单

| 模块/函数 | 计划改动 | 解决的问题 |
|---|---|---|
| `parsers/adapters/pdf_adapter.py::_parse_ragflow_with_tables` | 改为消费有序 Box；保留旧路径作为回退 | 避免标题扁平化、表格文末追加 |
| `parsers/adapters/pdf_adapter.py` 内部 Box Mapper | DeepDoc Box -> DocumentBlock | 集中字段映射，防止结构规则散落 |
| `parsers/ragflow/parser/pdf_parser.py::parse_into_bboxes` | 必要时补 Profile、Outline 和证据输出 | 让结构化入口具备生产可观测性 |
| DeepDoc LayoutRecognizer | 传递 `layout_score` | 保留标题候选的版面证据 |
| DeepDoc 字符聚合位置 | 删除 chars 前聚合字体样式 | 为数字 PDF 层级推断提供样式证据 |
| 新增 `parsers/pdf_heading_hierarchy.py` | 候选、Outline、编号、样式和冲突解析 | 输出可信 H1/H2/H3 |
| `chunking/document_sectionizer.py` | 仅把 `level>0` 当确认标题；建立父子路径 | 避免 level=0 title 被误判 H1 |
| 新增 `chunking/structure_recursive_chunker.py` | H1 分区、递归切分、装箱和 overlap | 实现目标切块行为 |
| `chunking/chunk_engine.py::_chunk_semantic` | PDF General 内部路由 | 保持公开枚举和默认行为兼容 |
| parse artifact / Chunk metadata | 保存证据、路径和来源 | 支持追踪、重放和质量分析 |

## 7. 验证方案

### 7.1 单元测试

#### Adapter

- 输入 `title -> text -> table -> text -> figure` Box，输出顺序完全一致；
- `title` 不再被无条件改写为 `layout_type=text`；
- table 和 figure 类型分别保留；
- page/bbox/positions/layoutno 正确映射；
- 空文本和特殊 Block 行为明确；
- 结构化路径失败时旧路径回退且 metadata 可追踪。

#### 标题候选

- DeepDoc title 成为候选；
- 同一 layoutno 的多行标题正确合并；
- 页眉、页脚、页码、表格标题、连续列表项被排除；
- 文档标题被标记为 `document_title`，不成为硬边界；
- 候选拒绝后正文内容不丢失。

#### Outline 匹配

- Outline 深度正确映射 H1/H2/H3；
- 页码、文本、Y 坐标联合匹配；
- 一对一约束有效；
- 未命中 Outline 不制造 Block；
- 部分 Outline 命中时，其余候选继续使用编号/样式回退。

#### 编号和样式

- 中文章/节/条、点分阿拉伯数字、罗马数字、英文 Chapter 等解析正确；
- 区分标题编号和连续列表；
- 子编号前缀、序列和重置逻辑正确；
- 仅出现 `1.1/1.2` 且无锚点时不自动提升为 H1；
- Outline、编号和样式冲突时降级；
- style-only 候选首期不生成硬 H1。

#### Sectionizer

- `level=0 + layout_type=title` 不被当作 H1；
- H1/H2/H3 父子关系和 heading path 正确；
- preamble、文档标题和跨页章节处理正确。

#### 递归 Chunker

- Chunk 不跨 H1；
- overlap 不跨 H1；
- `min_chunk_tokens` 合并不跨 H1；
- H2/H3 短节可以合并；
- 接近上限时优先在 H2/H3 前切分；
- 超长文本依次按段落、换行、句子、分号、逗号、空格和字符兜底；
- 分隔符保留且原文拼接后无丢失、无乱序；
- 表格和图片不按正文标点拆分；
- 没有 H1 时使用全文合成根递归分块；
- Token 上限按最终实际写入或用于 Embedding 的文本计算。

### 7.2 PDF 金标样本

至少覆盖：

- 有完整 Outline、部分 Outline、无 Outline；
- 中文、英文、中英混排；
- 数字 PDF、扫描 PDF、错误文本层 PDF；
- 带“章/节”、点分编号、无编号标题；
- 封面标题、目录页、重复页眉页脚、独立页码；
- 单栏、多栏、浮动图表；
- 正文中穿插表格和图片；
- 跨页表格；
- 单个超长段落、超长句子和无空格文本；
- 文档完全没有可靠 H1。

人工标注：

- 有序 Block；
- 标题候选；
- H1/H2/H3；
- 文档标题；
- 父节点；
- 表格/图片原始位置；
- 期望 H1 硬边界。

### 7.3 指标

#### 解析和结构指标

- 文本覆盖率、丢失率、重复率；
- Block 阅读顺序 pair accuracy；
- 表格/图片插入位置准确率；
- page/bbox 覆盖率；
- 标题候选 Precision/Recall；
- H1 Precision/Recall，优先保证 H1 Precision；
- H1/H2/H3 Macro-F1；
- parent relation accuracy；
- 文档标题误判 H1 比例。

#### 切块指标

- `cross_h1_chunk_count == 0`；
- `cross_h1_overlap_count == 0`；
- Chunk Token 长度分布；
- 超限 Chunk 数和原因；
- 原文字符覆盖、重复和乱序；
- Chunk 到 source block/page/bbox 的可追踪率；
- 无 H1 文档成功分块率。

#### RAG 效果指标

- Recall@K / Hit@K；
- MRR / NDCG；
- 答案准确率；
- 引用页码和 bbox 正确率；
- 数字 PDF 与扫描 PDF 分组表现；
- 解析耗时、峰值内存、入库总时延。

## 8. 上线顺序与回滚

推荐分四步上线：

1. **解析 Shadow**：同时生成旧扁平结果和新结构结果，只保存对比指标，不改变生产 Chunk。
2. **层级 Shadow**：运行标题 Resolver，记录候选、层级、证据和冲突，不启用 H1 边界。
3. **小范围 A/B**：指定 Workspace 使用结构递归 Chunk，重建这些文档的 Chunk 和向量索引。
4. **逐步默认开启**：指标通过后扩大范围，保留旧逻辑一个发布周期。

回滚只需要关闭结构递归开关并重新走旧 General 分块。已经按新策略写入的 Chunk 不会因关闭开关自动恢复，必须基于保存的解析 artifact 重新分块并重建向量索引。

## 9. 推荐实施顺序

每一步都应在验证通过后再进入下一步：

1. 建立少量人工 PDF Fixture 和结构断言；
2. Adapter 改为消费 `parse_into_bboxes()`，验证 Block 顺序和字段完整性；
3. 补齐 layout score、字体样式和 Outline 目标页等证据；
4. 实现标题候选识别，先验证候选 Precision/Recall；
5. 实现 Outline 对齐和全文编号体系；
6. 实现样式锚定、冲突降级和可信 H1 门控；
7. 修复 Sectionizer 的 `level=0` 标题语义并建立标题树；
8. 在人工构造的结构化 Block 上独立开发递归 Chunker；
9. 接入 `ChunkEngine._chunk_semantic()` 的 PDF General 内部路由；
10. 运行 Shadow、RAG 对比、灰度和索引重建。

不建议把“切换结构化 Adapter、标题层级识别、递归 Chunker、生产默认开启”放在一个不可拆分提交中。三个核心模块应分别可验证、可回退：

```text
DeepDoc structured adapter
        ↓
PdfHeadingHierarchyResolver
        ↓
GeneralStructuredRecursiveChunker
```

这三个 seam 分别隔离“解析格式差异”“标题推断复杂度”和“切块算法复杂度”，调用方只需要学习很小的接口，测试也可以在不执行 OCR 的情况下直接构造 Block 验证后两层。

## 10. 本方案明确不做的事情

- 不切换或新增 Docling Parser；
- 不替换现有业务 OCR；
- 不新增公开 `ChunkStrategy` 枚举；
- 不把所有 DeepDoc `title` 默认当作 H1；
- 不用 LLM 作为首期标题层级主判定器；
- 不在第一阶段为所有文档类型重建统一 AST；
- 不承诺仅靠 bbox 最近距离彻底解决所有复杂 PDF 阅读顺序；
- 不在没有可靠证据时伪造字体大小、标题级别或 bbox；
- 不自动修改历史索引，历史文档需要明确重放分块或重新解析入库。

## 11. 最终结论

本次改造不是简单地把“固定拼装”替换成一个递归字符串切分函数，而是先恢复切块所依赖的结构语义：

1. 使用 DeepDoc 已有的 `parse_into_bboxes()` 有序结构结果，避免 Adapter 再次扁平化标题、表格和图片；
2. 在 OpenRAG 内部结合 Outline、全文编号体系、字体/几何样式和版面证据，保守推断 H1/H2/H3；
3. 只有可信 H1 成为硬边界，H2-H6 只是优先切点；
4. 在每个硬边界区域内部，先按 DocumentBlock 装箱，单个 Block 超长时再按自然分隔符递归降级；
5. 没有可信 H1 时，以全文作为合成根运行同一递归算法；
6. 通过内部路由接入现有 `SEMANTIC`，保持生产 Worker、公开枚举和非目标文档类型兼容。

这样既能利用 DeepDoc 已经产生的版面信息，也能保持现有 OCR 资产不变，并把标题层级识别和递归切块分别放在清晰、可测试、可灰度回滚的模块 seam 后面。

## 12. 实施状态（2026-08-10）

已在 `fix-rank-bug` 分支完成首期代码实现：

- DeepDoc Adapter 优先消费 `parse_into_bboxes()`，保留有序的 title/text/table/figure Block；旧 `text_body + tbls` 接口仅作为不支持结构化方法时的兼容路径；
- DeepDoc 输出新增版面分数传递、数字 PDF 字体特征聚合、OCR 几何样式标记和带目标页码的 Outline；
- 新增 `PdfHeadingHierarchyResolver`，实现保守候选筛选、Outline 一对一匹配、中文/英文/点分编号推断、锚定样式继承、冲突降级及标题路径传播；
- Sectionizer 只把 `level>0` 视为确认标题，不再把 `level=0` 的 title 自动提升为 H1；
- 新增 General PDF 结构递归 Chunker，实现可信 H1 分区、H2-H6 软切点、递归分隔符降级、区域内 overlap、短尾安全合并、原子表格/图片和无 H1 全文兜底；
- `ChunkEngine` 保持公开 `SEMANTIC` 枚举不变，通过 `OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED` 控制内部路由，默认开启，设置为 `false` 可回退旧逻辑；
- 新增结构化 Adapter、标题 Resolver、Sectionizer 和递归 Chunker 契约测试，并加入仓库稳定测试收集范围。

当前实现只作用于显式选择 `deepdoc` 的 PDF 解析链路。仓库当前公开的 `pdf/auto` 路由仍使用 PaddleOCR Adapter，本次没有改变该生产 OCR 选择策略。
