# MD/TXT 原文位置映射设计

## 状态

设计稿，等待评审。本 spec 只描述方案，不包含实现改动。

## 目标

让 Markdown 和纯文本文件生成的 chunk 能准确映射回用户上传的原始文档内容。

对于 MD/TXT 文件，parser 产出的每个带位置信息的 `DocumentBlock` 都必须满足：

```python
block.text == raw_text[block.char_start:block.char_end]
```

这里的 `raw_text` 指用户上传文件解码后的原文字符流，且必须和前端原文预览使用同一个坐标系。这个坐标系是 UTF-8 解码后的 Python 字符串下标，不是字节偏移，也不是 Markdown 渲染后的 DOM 或可视文本位置。

## 当前问题

TXT 当前会读取上传文件并按段落切成 block，所以位置模型已经比较接近上传原文。主要风险是 TXT parser 使用 `errors="ignore"` 解码，而前端预览在遇到非法 UTF-8 时会使用替换字符。如果文件里存在非法字节，parser 和预览看到的字符流就可能不同，进而导致偏移不一致。

Markdown 当前虽然先读取上传原文，但随后调用 `RAGFlowMarkdownParser.extract_tables_and_remainder(md_text)`。后续逻辑会拆分处理后的 `remainder`，对段落做 `strip()`，再用 `md_text.find(...)` 把处理后的文本反查回原始 Markdown。表格还会被单独抽出并追加到普通段落之后。

这会带来几个位置映射风险：

- 处理后的文本可能已经不同于上传原文；
- 表格顺序可能不同于原文顺序；
- `strip()` 会改变 block 边界；
- 重复文本可能匹配到错误位置；
- `find()` 失败后的兜底逻辑会制造近似位置。

## 范围

本轮包含：

- 修正 MD parser 的原文 span 准确性。
- 修正 TXT parser 的原文 span 准确性。
- 增加 parser 层辅助函数，用于原文解码、行 span、边界裁剪和 block span 生成。
- 增加聚焦的后端测试，覆盖 parser span 和 chunk span 传播。

本轮不包含：

- DOC/DOCX 映射修复。
- PDF 位置映射变更。
- 前端渲染逻辑变更。
- 搜索结果跳转逻辑变更。
- 新增外部 parser 依赖。
- 映射到 Markdown 渲染后的视觉位置。

## 推荐方案

在 parser 层使用源码扫描器。

parser 应该直接扫描上传文件解码后的原文，并生成 `DocumentBlock`。每个 block 的 `text` 都必须是原文中的直接切片。parser 不应该先生成处理后的文本，再用搜索或模糊匹配回推原文位置。

这个方案简单且可验证：只要 block 文本始终是原文切片，chunk 从 block 继承或合并得到的位置就仍然处在上传原文坐标系中。

## 备选方案

### 方案一：使用带源码范围的 AST Parser

可以引入类似 tree-sitter-markdown 的 Markdown parser，由它输出语法节点和源码范围。

优点是 Markdown 语义边界更标准；缺点是依赖和构建复杂度更高，尤其在 Windows 环境下风险较大。另外这类工具通常返回 byte range，还需要额外做字节偏移到 Python 字符下标的转换。因此不建议作为第一版方案。

### 方案二：保留 RAGFlow 输出，再做文本对齐

可以继续使用 RAGFlow Markdown 处理结果，然后用 diff 或 fuzzy matching 把处理后文本对齐回原文。

优点是能保留现有 RAGFlow Markdown 输出；缺点是重复文本、空白裁剪、表格重排、语法归一化都会让对齐不稳定。这个方案很难保证源码级准确高亮，因此不推荐。

## 设计

### 原文 Span 辅助模块

新增一个轻量 helper，建议路径：

```text
openrag/src/openrag/parsers/source_spans.py
```

职责：

- 读取上传文本文件的 bytes。
- 优先使用严格 UTF-8 解码；如果失败，则用 `errors="replace"` 解码，保证 parser 坐标和前端预览行为一致。
- 使用 `splitlines(keepends=True)` 构建行 span，让每一行在解码后原文中都有稳定的 `[start, end)` 范围。
- 在需要裁剪 block 外侧空白时，同步调整 start/end，并保持 `text == raw[start:end]`。

建议提供的概念：

- `read_source_text(file_path: str) -> str`
- `LineSpan(text: str, start: int, end: int)`
- `iter_line_spans(raw: str) -> list[LineSpan]`
- `trim_span(raw: str, start: int, end: int) -> tuple[int, int]`
- `iter_plain_paragraph_spans(raw: str) -> Iterator[tuple[int, int]]`

该 helper 不负责 Markdown 语义识别。Markdown block 检测仍放在 Markdown adapter 中。

### TXT Parser

修改文件：

```text
openrag/src/openrag/parsers/adapters/txt_adapter.py
```

行为：

- 使用 `read_source_text(file_path)` 读取原文。
- 使用 `iter_plain_paragraph_spans(raw)` 将纯文本拆成段落 span。
- 每个段落生成一个 `DocumentBlock`，字段满足：
  - `text = raw[start:end]`
  - `char_start = start`
  - `char_end = end`
  - `block_type = "text"`
  - `layout_type = "text"`
  - `block_id = "txt:p:{idx}"`

段落拆分应把一个或多个空行视为分隔符。无论输入是 CRLF 还是 LF，输出位置都必须对应原始解码字符串中的真实下标。

### Markdown Parser

修改文件：

```text
openrag/src/openrag/parsers/adapters/markdown_adapter.py
```

行为：

- 使用 `read_source_text(file_path)` 读取原文。
- 停止使用 `extract_tables_and_remainder()` 构造 MD block。
- 逐行扫描原始 Markdown。
- 按原文顺序输出 block。
- 每个输出 block 都必须使用 `raw[start:end]` 作为 `text`。

第一版 Markdown block 规则：

- 空行分隔普通段落或列表 block。
- ATX 标题，也就是以 `#` 到 `######` 加空白开头的行，作为单独 heading block。
- 以三个反引号或三个波浪线开头的 fenced code block，从开始 fence 到结束 fence 作为一个 `code` block；如果没有结束 fence，则一直延伸到 EOF。
- GFM 风格管道表格：当某行后面紧跟合法 separator 行，例如 `| --- | --- |`，则把表头、separator 和后续管道行作为一个 `table` block。
- 其他连续非空行作为 `text` block。

标题层级：

- ATX 标题层级等于开头 `#` 的数量。
- 其他 block 的层级为 `0`。

block 元数据：

- MD/TXT 的 `page = 1`。
- `offset` 继续作为 parser block 的顺序编号，保持兼容。
- `block_id` 使用稳定的 parser 内 ID：
  - `md:heading:{idx}` 表示标题；
  - `md:p:{idx}` 表示普通段落或列表；
  - `md:table:{idx}` 表示表格；
  - `md:code:{idx}` 表示 fenced code block。

### Chunk Span 传播

现有 chunking 链路继续消费 `DocumentBlock.char_start/char_end`。

如果一个 chunk 只覆盖一个 parser block，则 chunk 的 source span 应位于该 block span 内。

如果一个 chunk 合并多个相邻 parser block，则 chunk 的 source span 可以覆盖从第一个 block 起点到最后一个 block 终点的连续原文范围。这个范围可能包含空行或 Markdown 语法标记，这是可以接受的，因为目标坐标系就是上传源码文本。

实现时应避免大范围修改 chunking。只有当测试证明现有 chunk span 传播无法满足 MD/TXT 原文坐标时，才做局部调整。若必须调整，应优先使用已有的 `source_blocks` 或覆盖 block span，而不是依赖文本搜索 fallback。

## 数据流

1. 上传接口把 MD/TXT 原始 bytes 存入 MinIO。
2. Worker 从 MinIO 下载原始文件到临时本地路径。
3. Parser 读取并解码原始文件，得到 `raw_text`。
4. Parser 扫描 `raw_text`，输出原文切片型 `DocumentBlock`。
5. Chunk engine 根据 block 生成 chunks，并传播 source span。
6. `DocumentProcessor` 将 `source_char_start/source_char_end` 写入 `document_chunks`。
7. 搜索接口和文档切块详情接口原样返回这些字段。
8. 前端原文预览使用 `raw_text[source_char_start:source_char_end]` 高亮目标内容。

## 错误处理

- 非法 UTF-8 不应默认导致解析失败。严格 UTF-8 解码失败时，parser 使用 replacement 解码，以匹配前端预览行为。
- 空文件返回空 block 列表。
- Markdown fenced code block 如果没有闭合 fence，则从开始 fence 一直作为 code block 延伸到 EOF。
- 不合法的表格形态不强行识别为 table，回退为普通 text block。

## 测试计划

增加或更新聚焦的后端测试。

建议测试文件：

- `openrag/tests/test_char_spans_chunk.py`
- `openrag/tests/test_parser_integration.py`，或新增更聚焦的 `openrag/tests/test_md_txt_source_spans.py`
- 只有在 chunk span 传播需要调整时，才修改 `openrag/tests/test_chunk_engine.py`

必须覆盖的测试场景：

- TXT parser 对 LF 段落保留准确 source span。
- TXT parser 对 CRLF 段落保留准确 source span。
- TXT parser 处理首尾空行时不产生偏移漂移。
- Markdown heading block 满足 `raw[start:end] == block.text`，且 heading level 正确。
- Markdown 普通段落或列表 block 满足 source slice invariant。
- Markdown fenced code block 包含 fence 行，并满足 source slice invariant。
- Markdown table block 保持原文顺序，并满足 source slice invariant。
- Markdown 重复文本不依赖 `find()`，能映射到正确的原文出现位置。
- Semantic chunking 写出的 `source_char_start/source_char_end` 可以用于切回原始 raw text。

定向校验命令：

```powershell
python -m pytest openrag/tests/test_char_spans_chunk.py openrag/tests/test_parser_integration.py openrag/tests/test_chunk_engine.py -q
```

如果新增了聚焦测试文件，需要把该文件加入定向校验命令。

## 验收标准

- 每个带 source metadata 的 MD/TXT parser block 都满足 `raw[char_start:char_end] == block.text`。
- Markdown 表格不会在 parser 输出中被移动到段落之后。
- Markdown parser 不再使用 `md_text.find(...)` 从处理后文本恢复位置。
- TXT parser 不再使用 `errors="ignore"`。
- 上传、worker、搜索和前端 API 契约不需要变化。
- 现有 PDF 映射行为不变。
- 新测试覆盖 MD/TXT 的原文 span 准确性。

## 风险

- Markdown 源码扫描器的语义精度会低于完整 Markdown AST parser。这个取舍可以接受，因为当前目标是准确映射回源码，而不是完整 Markdown 语法解析。
- Chunk 文本可能包含 Markdown 语法字符，例如标题 `#`、表格管道符、代码块 fence。这是预期行为，因为本轮目标是源码坐标映射。
- 如果下游检索质量依赖 RAGFlow Markdown 表格抽取，移除 MD adapter 中的表格抽离逻辑可能改变 chunk 文本形态。若后续发现检索质量受影响，可以再添加用于检索的 normalized text metadata，但 `DocumentBlock.text` 仍应保持原文切片。

## 实施约束

实现时保持改动克制：

- 不修改上传 API。
- 不修改前端预览。
- 不新增外部依赖。
- 本轮不修改 DOC/DOCX 行为。
- 除非 parser source span 不足以满足测试，否则不大范围修改 chunking。

最重要的约束是 source-slice equality，也就是 `text == raw[start:end]`。后续测试必须显式断言这一点，作为防止位置漂移复发的最小保护网。
