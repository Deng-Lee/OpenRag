# MD/TXT/DOCX Chunk 映射到 Canonical 文档实现方案

## 1. 总体目标

本功能将 MD/TXT/DOCX 的 chunk 定位目标从“上传原文”调整为“解析后、实际用于切 chunk 的 canonical 文档”。

目标数据关系：

```text
parser blocks
  -> canonical_text + normalized_blocks
  -> chunk engine
  -> document_chunks.source_char_start/source_char_end
  -> 前端 chunk-source canonical_text 高亮
```

上传原文仍用于下载和普通预览，不作为 MD/TXT/DOCX chunk 定位目标。

本轮明确不处理：

- 不调整 Markdown 表格抽取后的放置位置。
- 不引入外部 Markdown parser。
- 不新增数据库字段或迁移。
- 不改变 PDF 映射逻辑。

---

## 2. Canonical 规则

canonical 文档只对 MD/TXT/DOCX 生效。PDF 和其他类型保持现状。

输入不是上传原文，而是 parser 当前产出的 `DocumentBlock[]`。

生成规则：

1. 按 parser 当前输出顺序处理 blocks。
2. 不重新识别 Markdown 结构。
3. 不移动 Markdown 表格。
4. 每个 block 使用 `(block.text or "").strip()` 作为 canonical block 文本。
5. 空文本 block 跳过。
6. block 之间用单个 `\n` 拼接。
7. 不额外追加尾部换行。

坐标重写规则：

```python
normalized_block.text = canonical_block_text
normalized_block.char_start = start
normalized_block.char_end = start + len(canonical_block_text)
normalized_block.offset = start
```

每个 normalized block 必须满足：

```python
canonical_text[block.char_start:block.char_end] == block.text
```

规则不新增数据库字段。建议把规则标识写入 `canonical.json` metadata，例如：

```json
{
  "canonical_source": {
    "version": "chunk_source_v1",
    "applies_to": ["md", "txt", "docx"],
    "block_text": "strip",
    "joiner": "\\n",
    "preserve_parser_block_order": true,
    "preserve_parser_table_position": true,
    "trailing_newline": false
  }
}
```

数据库继续只保存 artifact 引用、parser 信息和 hash。

---

## 3. 步骤一：新增 canonical chunk source 生成模块

### 完成的功能

新增后端 helper，把 parser 输出的 `DocumentBlock` 列表转换成：

- `canonical_text`：真正用于切 chunk 的文本。
- `normalized_blocks`：`char_start/char_end` 已经改成 canonical 文本坐标的 blocks。

### 解决的问题

当前 `canonical.md` 是 parse artifact 阶段另行拼出来的展示文本，不一定和 chunk engine 实际切分文本坐标一致。该步骤让“切 chunk 的文本”和“前端展示的 canonical 文本”变成同一份文本。

### 涉及文件

新增：

- `openrag/src/openrag/services/canonical_chunk_source.py`

新增测试：

- `openrag/tests/test_canonical_chunk_source.py`

### 需要的函数、参数、字段

建议定义：

```python
from dataclasses import dataclass
from openrag.parsers.base import DocumentBlock

@dataclass
class CanonicalChunkSource:
    text: str
    blocks: list[DocumentBlock]

def supports_canonical_chunk_source(parser_name: str) -> bool:
    return parser_name in {
        "MarkdownParserAdapter",
        "TxtParserAdapter",
        "DocxParserAdapter",
    }

def build_canonical_chunk_source(blocks: list[DocumentBlock]) -> CanonicalChunkSource:
    ...
```

### 校验方式

测试断言：

```python
result = build_canonical_chunk_source(blocks)
assert result.text == "标题\n段落\n| A | B |"
assert result.text[result.blocks[1].char_start:result.blocks[1].char_end] == "段落"
```

### 符合预期的结果

- normalized block 的 `char_start/char_end` 都能准确切回 `canonical_text`。
- Markdown table block 的顺序和 parser 输入顺序一致，没有被 helper 移动。

---

## 4. 步骤二：扩展 ParseArtifactService 支持 canonical_text_override

### 完成的功能

让 parse artifact 可以直接写入步骤一生成的 `canonical_text`，而不是总是通过 `_canonical_markdown(blocks)` 重新拼接。

### 解决的问题

如果 `canonical.md` 仍由 `_canonical_markdown()` 生成，前端展示的 canonical 文档可能与 chunk engine 使用的文本不一致，offset 仍会偏移。

### 涉及文件

修改：

- `openrag/src/openrag/services/parse_artifact_service.py`
- `openrag/tests/test_parse_artifact_service.py`

### 需要的函数、参数、字段

修改函数：

```python
ParseArtifactService.persist_parse_artifacts(...)
```

新增可选参数：

```python
canonical_text_override: Optional[str] = None
```

内部逻辑：

```python
canonical_text = (
    canonical_text_override
    if canonical_text_override is not None
    else _canonical_markdown(canonical_blocks)
)
```

其余字段继续基于最终 `canonical_text` 计算：

- `canonical_text_hash`
- `canonical_md_size_bytes`
- `canonical_md_object_key`

### 校验方式

测试断言：

```python
result = service.persist_parse_artifacts(
    ...,
    blocks=blocks,
    canonical_text_override="A\nB",
)

md_object = storage.objects[(workspace.slug, result.canonical_md_object_key)]
assert md_object["data"].decode("utf-8") == "A\nB"
```

### 符合预期的结果

- 传入 override 时，`canonical.md` 内容完全等于 override。
- 不传 override 时，现有 PDF/其他类型行为保持不变。
- `canonical.json.blocks` 仍保存 block metadata 和 normalized 后的 `char_start/char_end`。

---

## 5. 步骤三：在 DocumentProcessor 中接入 canonical normalization

### 完成的功能

在 parse 后、chunk 前，对 MD/TXT/DOCX 的 blocks 做 canonical normalization，并让后续 parse artifact、chunk engine、embedding、document_chunks 都使用 normalized blocks。

### 解决的问题

这是整个功能的核心。它确保 `document_chunks.source_char_start/source_char_end` 指向 canonical 文档，而不是上传原文或 parser 内部临时文本。

### 涉及文件

修改：

- `openrag/src/openrag/processors/document_processor.py`
- `openrag/tests/test_document_processing_trace.py`

也可以新增更聚焦的测试：

- `openrag/tests/test_document_processor_canonical_source.py`

### 需要的函数、参数、字段

新增导入：

```python
from openrag.services.canonical_chunk_source import (
    build_canonical_chunk_source,
    supports_canonical_chunk_source,
)
```

处理流程：

```python
parsed_blocks = parser.parse(file_path)
canonical_text_override = None

if supports_canonical_chunk_source(_parser_name(parser)):
    canonical_source = build_canonical_chunk_source(parsed_blocks)
    text_blocks = canonical_source.blocks
    canonical_text_override = canonical_source.text
else:
    text_blocks = parsed_blocks
```

后续调用：

```python
ParseArtifactService(...).persist_parse_artifacts(
    ...,
    blocks=text_blocks,
    canonical_text_override=canonical_text_override,
)
```

chunk engine 也必须使用 `text_blocks`：

```python
chunks = self.chunk_engine.chunk(text_blocks, ...)
```

### 注意事项

- PDF 不进入 canonical normalization。
- 其他类型不进入 canonical normalization。
- Markdown 表格位置不调整，只按当前 parser 输出顺序进入 canonical 文档。
- parse 阶段 trace 中的 block count 应统计最终用于 chunk 的 `text_blocks`。

### 校验方式

使用 fake parser 或现有 parser 构造 blocks：

- 输入 blocks：`["A", "B", "表格"]`
- canonical text 应为 `"A\nB\n表格"`
- 生成 chunk 后，chunk 的 `source_char_start/source_char_end` 能在 canonical text 中切到对应内容。

### 符合预期的结果

- MD/TXT/DOCX 的 chunk span 与 `canonical.md` 对齐。
- PDF 测试不变。
- document processing trace 中仍能看到 `canonical.md` 被写入。

---

## 6. 步骤四：新增 chunk-source API

### 完成的功能

给前端提供一个专门读取 canonical chunk source 的接口。

### 解决的问题

当前前端只能读取上传原文 `/content` 或普通预览 `/preview`。但新方案中的 offset 指向 canonical 文档，如果前端继续展示上传原文，就会再次坐标不一致。

### 涉及文件

修改：

- `openrag/src/openrag/api/workspace_file_api.py`
- `openrag/tests/test_workspace_file_api.py`

### 接口定义

新增 endpoint：

```http
GET /workspaces/{workspace_id}/files/{file_id}/chunk-source
```

复用现有 response model：

```python
class WorkspaceFilePreviewResponse(BaseModel):
    format: str
    content: str
```

返回：

```json
{
  "format": "text",
  "content": "canonical text..."
}
```

### 需要用到的模型/字段

查询 `DocumentParseArtifact`：

- `file_id`
- `workspace_id`
- `status == "completed"`
- `canonical_md_bucket`
- `canonical_md_object_key`
- `updated_at` 或 `created_at` 用于选最新 artifact

读取 MinIO：

```python
MinioStorage().read_object_bytes(
    artifact.canonical_md_bucket,
    artifact.canonical_md_object_key,
)
```

### 错误处理

- 文件不存在或无权限：沿用 `get_readable_workspace_file_or_404`
- 目录文件：400
- 无 completed artifact：404
- MinIO 读取失败：404，提示 canonical source 不存在

### 校验方式

测试场景：

- 有 completed artifact：返回 `format == "text"`，`content` 等于 canonical.md。
- 无 artifact：404。
- artifact 不属于 workspace/file：不能读到。
- 目录：400。
- 无权限：403，沿用现有权限逻辑。

### 符合预期的结果

前端能够通过独立接口拿到 canonical 文本，而不会混用上传原文。

---

## 7. 步骤五：前端 API 层增加 fetchWorkspaceChunkSource

### 完成的功能

在前端 service 中封装新接口。

### 解决的问题

避免组件直接拼 URL，保持现有 `filesAPI` 风格一致。

### 涉及文件

修改：

- `web/src/services/api.ts`
- `web/src/services/api.test.ts`

### 新增函数

```ts
fetchWorkspaceChunkSource: async (
  workspaceId: number,
  fileId: number
): Promise<{ format: 'text'; content: string }> => {
  const response = await api.get(
    `/workspaces/${workspaceId}/files/${fileId}/chunk-source`
  );
  return response.data as { format: 'text'; content: string };
}
```

### 校验方式

API test 断言：

```ts
expect(mockedAxios.get).toHaveBeenCalledWith(
  '/workspaces/7/files/9/chunk-source'
);
```

### 符合预期的结果

- 请求路径正确。
- 返回类型能被 `DocumentSourcePreview` 直接使用。

---

## 8. 步骤六：前端 DocumentSourcePreview 优先展示 canonical 文档

### 完成的功能

在切块详情页中，MD/TXT/DOCX chunk 默认展示 canonical chunk source，并在 canonical 文本里高亮 chunk。

### 解决的问题

让前端展示文本和后端 `source_char_start/source_char_end` 指向同一份文本，彻底避免“chunk 坐标指向 A，页面展示 B”的偏移。

### 涉及文件

修改：

- `web/src/components/document-source-preview/index.tsx`
- `web/src/components/document-source-preview/index.test.tsx`

### 需要新增/调整的逻辑

新增 state：

```ts
const [chunkSourceText, setChunkSourceText] = useState<string | null>(null);
const [usingChunkSource, setUsingChunkSource] = useState(false);
```

判断哪些类型优先用 chunk-source：

```ts
const shouldUseChunkSource =
  chunk != null &&
  workspaceId != null &&
  (cat === 'markdown' || cat === 'text' || cat === 'office');
```

请求逻辑：

```ts
if (shouldUseChunkSource) {
  try {
    const src = await filesAPI.fetchWorkspaceChunkSource(workspaceId, fileId);
    if (cancelled) return;
    setChunkSourceText(src.content);
    setUsingChunkSource(true);
    setLoading(false);
    return;
  } catch {
    setUsingChunkSource(false);
  }
}
```

渲染逻辑：

```tsx
{usingChunkSource && chunkSourceText != null && chunk ? (
  <pre ...>
    {renderTextWithNav(chunkSourceText, chunk, instanceDomId)}
  </pre>
) : ...原逻辑}
```

### Python offset 与 JS offset 差异

后端 `source_char_start/source_char_end` 是 Python 字符串 code point 下标；前端 `slice()` 是 UTF-16 code unit 下标。为避免 emoji 等字符导致偏移，需要在 `renderTextWithNav` 内转换：

```ts
function codePointOffsetToUtf16Index(text: string, offset: number): number {
  if (offset <= 0) return 0;
  return Array.from(text).slice(0, offset).join('').length;
}
```

使用：

```ts
const rawS = getChunkStartOffset(chunk) ?? 0;
const rawE = getChunkEndOffset(chunk) ?? 0;
const s = codePointOffsetToUtf16Index(text, rawS);
const e = codePointOffsetToUtf16Index(text, rawE);
```

### 校验方式

前端测试：

- TXT canonical source 返回 `"alpha beta gamma"`，chunk span 为 `6..10`，应出现 `.chunk-text-highlight`，内容为 `beta`。
- chunk-source 失败时，显示原 preview/content，但 `.chunk-text-highlight` 不出现。
- canonical 文本包含 emoji 时，span 仍能高亮正确内容。
- PDF 测试保持不变。

### 符合预期的结果

- MD/TXT/DOCX 切块详情中默认显示 canonical 文本。
- chunk 可以准确高亮。
- 旧文件没有 canonical artifact 时不会错高亮上传原文。

---

## 9. 步骤七：更新文档/spec，明确新语义

### 完成的功能

把现有 spec 从“映射上传原文”更新为“映射 canonical chunk source”。

### 解决的问题

避免后续实现者继续按照旧目标修 MD parser 原文 span，造成方向偏差。

### 涉及文件

修改：

- `docs/superpowers/specs/2026-06-02-md-txt-source-span-design.md`

### 需要明确写入的内容

- MD/TXT/DOCX 默认映射到解析后切分源文档。
- 上传原文只用于下载和普通预览。
- Markdown 表格抽取后的位置本轮不修改。
- canonical 文档按当前 parser block 输出顺序生成。
- chunk-source 缺失时前端回退普通预览，但不使用 canonical offset 高亮。

### 校验方式

人工检查文档中不再出现“必须映射回上传原文源码位置”作为本轮目标。

### 符合预期的结果

设计文档和实现计划一致。

---

## 10. 步骤八：后端集成验证

### 完成的功能

确认后端 canonical source、artifact、chunk span、API 全链路一致。

### 建议命令

```powershell
python -m pytest openrag/tests/test_canonical_chunk_source.py -q
python -m pytest openrag/tests/test_parse_artifact_service.py -q
python -m pytest openrag/tests/test_workspace_file_api.py -q
python -m pytest openrag/tests/test_document_processing_trace.py -q
```

如果 processor 测试新增在独立文件：

```powershell
python -m pytest openrag/tests/test_document_processor_canonical_source.py -q
```

### 符合预期的结果

- 所有测试通过。
- canonical.md 内容等于 chunk source。
- document_chunks 的 source span 能切回 canonical.md。
- PDF 相关测试无回归。

---

## 11. 步骤九：前端集成验证

### 完成的功能

确认切块详情页展示 canonical 文档并高亮 chunk。

### 建议命令

```powershell
cd web
.\node_modules\.bin\vitest.cmd --run src/components/document-source-preview/index.test.tsx src/services/api.test.ts --reporter verbose
npm test
npm run build
```

### 符合预期的结果

- chunk-source API 封装测试通过。
- MD/TXT/DOCX canonical 高亮测试通过。
- chunk-source 失败回退测试通过。
- PDF 预览测试继续通过。
- 前端构建成功。

---

## 12. 最终验收场景

### 场景一：TXT

上传 TXT，处理完成后进入切块详情。

预期：

- 预览区域显示 canonical 文本。
- 点击 chunk 后高亮位置准确。

### 场景二：MD，包含普通段落和表格

上传包含普通段落和表格的 MD。

预期：

- canonical 文档包含表格。
- 表格位置按当前 parser 输出顺序，不要求恢复上传原文位置。
- chunk 高亮准确定位到 canonical 文档。

### 场景三：DOCX

上传 DOCX。

预期：

- canonical 文档是 DOCX parser 抽取后的文本。
- chunk 高亮准确定位到 canonical 文档。
- 上传原 DOCX 仍可通过下载/普通预览查看。

### 场景四：旧文件无 parse artifact

打开没有 parse artifact 的旧文件。

预期：

- 切块详情回退普通预览。
- 不出现错误高亮。
- 重新处理后可使用 canonical source。

---

## 13. 最终检查命令

```powershell
git diff --check
```

如时间允许，再跑：

```powershell
python -m pytest openrag/tests -q
cd web
npm test
npm run build
```

---

## 14. 默认假设

- 不新增数据库字段，不做迁移。
- 不引入外部 Markdown parser。
- 不改变 Markdown 表格抽取后的放置位置。
- 不改变 PDF 映射逻辑。
- canonical 文档以纯文本展示，不做 Markdown 渲染。
- 旧文件需要重新处理后才有准确 canonical 映射。
