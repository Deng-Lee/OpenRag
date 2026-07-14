# PaddleOCR 接入 OpenRag Execution Plan

> 历史说明：本文记录最初“显式 `paddleocr`、DeepDoc 默认”的实施过程。当前公开契约已由 `paddleocr_openrag_integration_plan.md` 中的最终方案替代；下方旧公开值仅用于追溯，不可作为当前 API 用法。

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不影响现有 DeepDoc PDF 链路的前提下，新增用户显式选择的 PaddleOCR PDF 解析能力，并保留 PDF chunk 点击回原文的 block 级定位能力。

**Architecture:** 后端新增独立 `PaddleOCRPDFParserAdapter`，只在 `parser_type=paddleocr` 时由 `ParserFactory` 直连启用；`auto`、`pdf` 和 `.pdf` 默认扩展名解析继续走 `PDFParserAdapter`/DeepDoc。PaddleOCR 只负责解析 PDF 并输出 `DocumentBlock`，后续 chunk、embedding、持久化、搜索和 PDF 回源点击继续复用现有 OpenRag 流程。

**Tech Stack:** Python、FastAPI、SQLAlchemy、pytest、requests、React、TypeScript、Ant Design、Vitest、Docker Compose、Kubernetes。

---

## 执行边界

- PaddleOCR 仅支持 PDF，且只在用户显式选择 `parser_type=paddleocr` 时启用。
- PaddleOCR 失败不自动降级 DeepDoc，避免用户无法判断解析结果来源。
- PaddleOCR v1 暂不生成行级 `metadata["line_positions"]`；点击 chunk 回原文时使用 block 级 `bbox`。
- PaddleOCR 的 `title`、`doc_title` 等 label 不映射为 OpenRag 标题节点，统一按 `block_type="text"`、`level=0` 输出，原始 label 写入 `metadata["ocr_label"]`。
- 不修改现有 `PDFParserAdapter`、DeepDoc parser、`openrag/rag/*` 旧链路。若实现时发现必须修改这些逻辑，先停止并确认。

## 文件清单

- Create: `openrag/src/openrag/parsers/adapters/paddleocr_pdf_adapter.py`
- Create: `openrag/tests/test_paddleocr_pdf_adapter.py`
- Create: `openrag/tests/test_paddleocr_pdf_integration.py`
- Modify: `openrag/src/openrag/parsers/factory.py`
- Modify: `openrag/src/openrag/chunking/chunk_params.py`
- Modify: `openrag/src/openrag/services/file_ingest.py`
- Modify: `openrag/src/openrag/api/files_api.py`
- Modify: `openrag/src/openrag/api/service_api.py`
- Modify: `web/src/components/FileUpload.tsx`
- Modify: `web/src/components/FileList.tsx`
- Modify: `web/src/services/api.ts`
- Modify: `web/src/types/index.ts`
- Modify: `web/src/i18n/locales/zh.json`
- Modify: `web/src/i18n/locales/en.json`
- Modify: `web/src/components/FileList.test.tsx`
- Modify: `web/src/services/api.test.ts`
- Modify: `docker/.env.example`
- Modify: `docker/.env.external-192.168.100.33.example`
- Modify: `docker/docker-compose.prod.yml`
- Modify: `docker/docker-compose.worker.yml`
- Modify: `k8s/13-configmap-openrag-llm.yaml`
- Modify: `k8s/10-task-worker.yaml`
- Modify: `docs/04-外部系统接入与API.md`

---

### Task 1: 后端 parser_type 入口与安全边界

**处理的问题**

让后端所有上传、替换、upsert、重处理入口接受 `paddleocr`，同时保证非 PDF 不能误触发 PaddleOCR，且默认 `auto`/`pdf` 行为不变。

**需要修改**

- 在 `openrag/src/openrag/services/file_ingest.py` 的 `SUPPORTED_PARSER_TYPES` 增加 `paddleocr`。
- 在 `resolve_effective_mime_type()` / `is_processing_supported()` 相关逻辑中，将 `parser_type=paddleocr` 限制为 PDF。
- 在 `openrag/src/openrag/api/files_api.py` 的 `ReprocessRequest` 校验中复用更新后的 `SUPPORTED_PARSER_TYPES`。
- 检查 `openrag/src/openrag/api/service_api.py` 中 service-token 上传、替换、upsert 是否均通过 `file_ingest.py` 校验；若存在绕过校验的入口，补同样的 PDF 限制。
- 不新增数据库迁移，继续复用 `files.parser_type`。

**需要新增**

- 在 `openrag/tests/test_paddleocr_pdf_integration.py` 新增 parser_type 校验测试：
  - `paddleocr` 是合法 parser type。
  - `paddleocr + pdf` 被允许。
  - `paddleocr + docx/txt/xlsx` 被拒绝。
  - `auto + pdf` 仍被允许。
  - `pdf + pdf` 仍被允许。

**如何验证**

Run:

```powershell
pytest openrag/tests/test_paddleocr_pdf_integration.py -q
```

Expected:

```text
all tests pass
```

同时运行现有入口相关测试：

```powershell
pytest openrag/tests -q -k "file_ingest or reprocess or parser_type"
```

Expected:

```text
no regression caused by adding paddleocr
```

---

**执行结果**

- 状态：已完成
- Coding：完成 parser_type=paddleocr 后端入口和 PDF-only 安全边界。
- Testing：`openrag\venv\Scripts\python.exe -m pytest openrag/tests/test_paddleocr_pdf_integration.py -q` 通过；`openrag\venv\Scripts\python.exe -m pytest openrag/tests -q -k "file_ingest or reprocess or parser_type"` 通过。
- Review：确认未修改 DeepDoc/PDFParserAdapter/旧链路；service-token 入口共享 file_ingest 校验；无阻塞风险。

### Task 2: ParserFactory 隔离路由

**处理的问题**

让 PaddleOCR 只在用户显式选择时生效，不抢占 `.pdf` 默认解析器，确保 DeepDoc 原链路无感。

**需要修改**

- 在 `openrag/src/openrag/parsers/factory.py` 中新增一个按 `parser_type` 直连 adapter 的小映射，例如只包含 `paddleocr -> PaddleOCRPDFParserAdapter`。
- `get_parser_by_type("paddleocr")` 返回 `PaddleOCRPDFParserAdapter()`。
- `get_parser_by_extension(".pdf")` 继续返回 `PDFParserAdapter()`。
- `_parser_type_map["pdf"]` 继续映射 `.pdf`，不把 `.pdf` 改成 PaddleOCR。

**需要新增**

- 在 `openrag/tests/test_paddleocr_pdf_integration.py` 新增路由测试：
  - `ParserFactory().get_parser_by_type("paddleocr")` 是 PaddleOCR adapter。
  - `ParserFactory().get_parser_by_type("pdf")` 仍是 `PDFParserAdapter`。
  - `ParserFactory().get_parser("sample.pdf")` 仍是 `PDFParserAdapter`。
  - `ParserRegistry().get_parser("sample.pdf", "auto")` 仍是 `PDFParserAdapter`。

**如何验证**

Run:

```powershell
pytest openrag/tests/test_parser_factory.py openrag/tests/test_paddleocr_pdf_integration.py -q
```

Expected:

```text
existing parser factory tests still pass
paddleocr direct route tests pass
```

**执行结果**

- 状态：已完成
- Coding：完成 `parser_type=paddleocr` 到 `PaddleOCRPDFParserAdapter` 的显式直连路由，并保持 `.pdf` / `pdf` / `auto` 继续走 DeepDoc `PDFParserAdapter`。
- Testing：`openrag\venv\Scripts\python.exe -m pytest openrag/tests/test_parser_factory.py openrag/tests/test_paddleocr_pdf_integration.py -q` 通过。
- Review：确认当前仅新增 PaddleOCR adapter 壳，未实现 OCR HTTP 协议，未读取 `PADDLEOCR_SERVER_URL`，未修改 DeepDoc/PDFParserAdapter 默认链路。

---

### Task 3: PaddleOCR PDF Adapter

**处理的问题**

实现 OpenRag 内部 adapter，按自部署 PaddleOCR 异步协议提交 PDF、轮询结果、转换为 `DocumentBlock`。实现不能依赖 `E:\project\OCR接入` 的运行时路径。

**需要修改**

- 不修改现有 DeepDoc adapter。
- 新 adapter 使用环境变量 `PADDLEOCR_SERVER_URL`，代码不写死内网或外网默认 URL。
- 未配置 `PADDLEOCR_SERVER_URL` 且用户选择 PaddleOCR 时，抛出清晰错误，只失败当前文件任务。
- HTTP 调用使用已在项目中使用的 `requests`，设置提交和轮询 timeout。
- 轮询需要有最大等待时间或最大次数，避免 worker 无限等待。

**需要新增**

- 新增 `openrag/src/openrag/parsers/adapters/paddleocr_pdf_adapter.py`。
- adapter 的核心行为：
  - `POST {PADDLEOCR_SERVER_URL}/paddleocr/async/ocr`，multipart 上传 PDF。
  - `GET {PADDLEOCR_SERVER_URL}/paddleocr/async/task/{task_id}` 轮询。
  - 支持状态 `done`、`failed`、`not_found` 和超时。
  - 解析 `result.pages[].parsing_res_list`。
  - 兼容字段 `content` / `block_content`、`bbox` / `block_bbox`、`label` / `block_label`。
- `DocumentBlock` 输出规则：
  - 空文本 block 跳过。
  - `bbox` 必须规范化为 4 个 float，否则置为 `None` 并计入 `invalid_bbox`。
  - 页码按响应页顺序 1-based。
  - `table` label -> `block_type="table"`、`level=0`。
  - 其它 label，包括 `title`、`doc_title`、`text`、`header`、`footer` -> `block_type="text"`、`level=0`。
  - 原始 label 写入 `metadata["ocr_label"]`。
  - v1 不写 `metadata["line_positions"]`。
  - `char_start` / `char_end` 按输出文本流累加。
  - `block_id` 使用 `paddleocr:page:{page}:block:{index}`。
  - 最终继续调用 `to_document_blocks(raw_rows, source="paddleocr")`，复用现有字段转换与 bbox 保护。

**如何验证**

在 `openrag/tests/test_paddleocr_pdf_adapter.py` 中通过 monkeypatch fake `requests.post` / `requests.get`，不访问真实 PaddleOCR 服务。

Run:

```powershell
pytest openrag/tests/test_paddleocr_pdf_adapter.py -q
```

Expected:

```text
success response creates DocumentBlock list
title/doc_title stay text level 0 and keep metadata["ocr_label"]
table creates block_type table
invalid bbox becomes None
missing PADDLEOCR_SERVER_URL raises clear error
remote failed/not_found/timeout raises clear error
```

**执行结果**

- 状态：已完成
- Coding：完成 PaddleOCR PDF adapter，支持提交异步 OCR、轮询任务、转换 `DocumentBlock`，并保持 `title/doc_title` 按 text/level=0 输出。
- Testing：`openrag\venv\Scripts\python.exe -m pytest openrag/tests/test_paddleocr_pdf_adapter.py -q` 通过。
- Review：确认未依赖 `E:\project\OCR接入` 运行时路径，未修改 DeepDoc/PDFParserAdapter/旧链路；v1 未生成 `line_positions`，点击回源后续按 block 级 bbox 复用。

---

### Task 4: OCR 耗时日志与 trace profile

**处理的问题**

让用户能看到 PaddleOCR 处理文档的耗时，且能区分耗时发生在提交、远端等待、结果读取还是本地转换。

**需要修改**

- `PaddleOCRPDFParserAdapter` 内维护 `last_parse_profile`，字段结构与现有 `PDFParserAdapter` 可被 `DocumentProcessor._parse_span_profile()` 读取。
- 如需让 trace 输出包含 `block_count`、`poll_count`、`ocr_task_id`，在 `openrag/src/openrag/processors/document_processor.py` 的 `_parse_span_profile()` 中仅做兼容性透传，不改变 DeepDoc 字段含义。

**需要新增**

- 结构化日志事件：
  - `paddleocr.submit`：记录提交 PDF 到 OCR 服务耗时、HTTP 状态、`ocr_task_id`。
  - `paddleocr.poll`：记录轮询总耗时、`poll_count`、最终 OCR 状态。
  - `paddleocr.fetch_result`：记录从轮询响应中提取并校验 result 的耗时、页数。
  - `paddleocr.convert_blocks`：记录 PaddleOCR result 转 `DocumentBlock` 的耗时、`block_count`、`n_tables`、`skipped_empty`、`invalid_bbox`。
- `last_parse_profile` 至少包含：
  - `total_ms`
  - `page_count`
  - `block_count`
  - `n_tables`
  - `poll_count`
  - `ocr_task_id`
  - `status`
  - `stages`

**如何验证**

Run:

```powershell
pytest openrag/tests/test_paddleocr_pdf_adapter.py openrag/tests/test_document_processing_trace.py openrag/tests/test_parse_profile_persistence.py -q
```

Expected:

```text
adapter success and failure both expose last_parse_profile
parse.document.output_summary.pdf_stage_profile contains paddleocr stages
existing DeepDoc parse profile tests still pass
```

---

**执行结果**

- 状态：已完成
- Coding：完成 PaddleOCR 四阶段结构化耗时日志和 `last_parse_profile`，成功/失败均保留 profile；远端错误摘要已脱敏，避免 OCR 文本进入日志、profile 或上层 trace 异常。
- Testing：`openrag\venv\Scripts\python.exe -m pytest openrag/tests/test_paddleocr_pdf_adapter.py openrag/tests/test_document_processing_trace.py openrag/tests/test_parse_profile_persistence.py -q` 通过。
- Review：确认 `_parse_span_profile()` 仅兼容透传 PaddleOCR 扩展字段，DeepDoc 既有 profile 字段含义不变；未修改 DeepDoc/PDFParserAdapter/旧链路。

### Task 5: PDF chunk 路由与点击回原文能力

**处理的问题**

PaddleOCR 形成 `DocumentBlock` 后，需要继续走 PDF 的分块和定位链路，保证 chunk 点击后能映射回 PDF 页面和 block 级 bbox。

**需要修改**

- 在 `openrag/src/openrag/chunking/chunk_params.py` 中让 `resolve_chunk_method(file_path, "paddleocr")` 返回 `pdf_manual`。
- 不改 `ChunkEngine`、`document_chunks` 表和前端 PDF 点击逻辑。

**需要新增**

- 在现有 chunk routing 测试或 `openrag/tests/test_paddleocr_pdf_integration.py` 中新增：
  - `resolve_chunk_method("x.pdf", "paddleocr") == "pdf_manual"`。
  - 使用 PaddleOCR adapter 产出的 block 经过 chunk 后，chunk 包含 `page`、`bbox`、`source_block_id`、`source_char_start`、`source_char_end`。

**如何验证**

Run:

```powershell
pytest openrag/tests/parity/test_orchestration_parity.py openrag/tests/test_chunk_engine.py openrag/tests/test_paddleocr_pdf_integration.py -q
```

Expected:

```text
paddleocr PDFs use pdf_manual
chunks persist page and bbox-compatible fields
existing PDF/manual chunk behavior still passes
```

验收说明：

- v1 不做 `line_positions`，因此点击回源高亮为 block 级区域。
- 只要 `DocumentBlock.page` 与 `DocumentBlock.bbox` 坐标正确，现有回源链路可复用。

**执行结果**

- 状态：已完成
- Coding：完成 `parser_type=paddleocr` 到 `pdf_manual` 的 chunk 路由，并新增 block 级 `page/bbox/source_*` 回源字段验证。
- Testing：`openrag\venv\Scripts\python.exe -m pytest openrag/tests/parity/test_orchestration_parity.py openrag/tests/test_chunk_engine.py openrag/tests/test_paddleocr_pdf_integration.py -q` 通过。
- Review：确认未修改 `ChunkEngine`、`document_chunks` 表或前端 PDF 点击逻辑；PaddleOCR v1 继续按 block 级 bbox 复用现有回源链路。

---

### Task 6: API 响应与前端可选择解析器

**处理的问题**

用户需要在上传和重处理时自主选择 PaddleOCR 或 DeepDoc。前端还需要能在重处理弹窗中回显当前 parser。

**需要修改**

- `openrag/src/openrag/api/files_api.py` 的文件响应模型可增加可选 `parser_type` 字段，用于前端回显。
- `web/src/types/index.ts` 的 `File` 类型增加可选 `parser_type?: string | null`。
- `web/src/components/FileUpload.tsx` 的 `PARSER_TYPES` 增加 `paddleocr`。
- `web/src/components/FileList.tsx` 的重处理 parser 下拉增加 `paddleocr`。
- `FileList.tsx` 打开重处理弹窗时，优先使用 `record.parser_type || "auto"`。
- 文件夹上传继续固定 `auto`，不提供 PaddleOCR 批量目录选择。
- `web/src/i18n/locales/zh.json` 和 `web/src/i18n/locales/en.json` 增加 `files.parser_types.paddleocr`。

**需要新增**

- 前端测试新增或调整：
  - 上传下拉展示 PaddleOCR。
  - 重处理下拉展示 PaddleOCR。
  - `filesAPI.upload(..., "paddleocr", ...)` 会发送 `parser_type=paddleocr`。
  - `filesAPI.reprocess(id, "paddleocr", ...)` 会发送 `{ parser_type: "paddleocr" }`。

**如何验证**

Run:

```powershell
cd web
npm test -- --run
```

Expected:

```text
FileUpload / FileList / api tests pass
```

必要时运行构建：

```powershell
cd web
npm run build
```

Expected:

```text
build succeeds
```

**执行结果**

- 状态：已完成
- Coding：完成后端 `FileResponse.parser_type` 与 `_file_to_response()` 回填；完成前端上传/重处理解析器下拉的 `PaddleOCR` 选项、重处理弹窗按 `record.parser_type || "auto"` 回显、`File` 类型与中英文 i18n 补充；文件夹上传继续固定 `auto`。
- Testing：`cd web; npm test` 通过（16 个测试文件、117 个测试）；`cd web; npm run build` 通过；`openrag\venv\Scripts\python.exe -m pytest openrag\tests\test_files_api.py::TestFileGet::test_get_file_includes_parser_type -q` 通过。计划中的 `npm test -- --run` 在当前 `package.json` 已包含 `vitest --run` 时会重复传参失败，已用等价命令 `npm test` 验证。
- Review：确认新增响应字段向后兼容；上传和重处理均可选择 PaddleOCR；历史文件缺失 `parser_type` 时回落 `auto`；文件夹上传未暴露 PaddleOCR；未触碰 DeepDoc/PDFParserAdapter/openrag/rag 旧链路；无风险、无歧义、无差错。

---

### Task 7: 部署配置区分内网和外网

**处理的问题**

内网和外网 PaddleOCR 服务地址不同，代码不能写死地址，需要由部署环境注入。

**需要修改**

- `docker/.env.example` 增加注释形式的 `PADDLEOCR_SERVER_URL` 示例。
- `docker/.env.external-192.168.100.33.example` 增加外网地址：
  - `PADDLEOCR_SERVER_URL=http://litellm.guozhijishu.com`
- `docker/docker-compose.prod.yml` 的 `task-worker` 环境变量增加：
  - `PADDLEOCR_SERVER_URL=${PADDLEOCR_SERVER_URL:-}`
- `docker/docker-compose.worker.yml` 的 `task-worker` 环境变量增加同样配置。
- `k8s/13-configmap-openrag-llm.yaml` 增加内网地址：
  - `PADDLEOCR_SERVER_URL: "http://litellm.dev.guozhijishu.com"`
- `k8s/10-task-worker.yaml` 将 ConfigMap 中的 `PADDLEOCR_SERVER_URL` 注入 worker 容器。

**需要新增**

- 不新增运行时代码分支。
- 不给 API 容器注入该变量，除非后续新增 API 侧健康检查；本次 OCR 调用只发生在 worker。

**如何验证**

Run:

```powershell
docker compose -f docker/docker-compose.prod.yml config
```

Expected:

```text
task-worker environment contains PADDLEOCR_SERVER_URL
```

Run:

```powershell
kubectl apply --dry-run=client -f k8s/13-configmap-openrag-llm.yaml
kubectl apply --dry-run=client -f k8s/10-task-worker.yaml
```

Expected:

```text
both manifests validate successfully
```

**执行结果**

- 状态：已完成
- Coding：完成 `docker/.env.example` 注释示例、外网 `.env.external-192.168.100.33.example` 地址、Docker Compose worker 环境变量、K8s 内网 ConfigMap 与 worker env 注入；未给 API 容器注入该变量，未新增运行时代码分支。
- Testing：`docker compose -f docker/docker-compose.prod.yml config` 与 `docker compose -f docker/docker-compose.worker.yml config` 通过，确认 `task-worker` 含 `PADDLEOCR_SERVER_URL` 且 API 服务未注入；`kubectl kustomize k8s` 通过；`kubectl apply --dry-run=client ...` 因本机无 Kubernetes API 连接 `localhost:8080` 失败，属于环境限制，已用 kustomize 与静态检查替代验证。
- Review：确认 Docker/K8s 已按内外网分别注入 `PADDLEOCR_SERVER_URL`；仅 worker 获得该变量；未发现运行时代码硬编码内外网 URL 或新增内外网判断；无风险、无歧义、无差错。

---

### Task 8: 外部 API 文档与用户说明

**处理的问题**

API 调用方需要知道 `parser_type=paddleocr` 的使用方式和限制，避免误以为 PaddleOCR 是默认 PDF 解析器。

**需要修改**

- 在 `docs/04-外部系统接入与API.md` 的 parser type 说明中增加 `paddleocr`。
- 明确：
  - `auto` / `pdf` 仍走 DeepDoc。
  - `paddleocr` 仅支持 PDF。
  - PaddleOCR 失败不会自动降级 DeepDoc。
  - PaddleOCR v1 点击回源使用 block 级 bbox，不做行级 `line_positions`。
  - 内外网地址由 worker 的 `PADDLEOCR_SERVER_URL` 控制。

**需要新增**

- 增加一个上传示例：

```bash
curl -F "file=@sample.pdf" \
  -F "parser_type=paddleocr" \
  http://<openrag-api>/files/upload
```

- 增加一个重处理示例：

```json
{
  "parser_type": "paddleocr"
}
```

**如何验证**

Run:

```powershell
Select-String -LiteralPath 'docs/04-外部系统接入与API.md' -Pattern 'paddleocr','PaddleOCR','parser_type'
```

Expected:

```text
documentation mentions paddleocr parser type and PDF-only constraint
```

**执行结果**

- 状态：已完成
- Coding：在 `docs/04-外部系统接入与API.md` 补充 `parser_type=paddleocr` 用法与限制，说明 `auto`/`pdf` 仍走 DeepDoc、PaddleOCR 仅支持 PDF、失败不自动降级、v1 使用 block 级 `bbox` 回源且暂不做 `line_positions`、服务地址由 worker 的 `PADDLEOCR_SERVER_URL` 控制；新增 JWT multipart 上传示例、service-token 上传示例和重处理 JSON 示例。
- Testing：`Select-String -LiteralPath 'docs/04-外部系统接入与API.md' -Pattern 'paddleocr','PaddleOCR','parser_type','workspace_id','Authorization'` 通过；确认文档覆盖 PaddleOCR parser type、PDF-only、DeepDoc 默认链路、失败不降级、回源边界、环境变量控制和上传/重处理示例。
- Review：初审发现 JWT 上传示例缺少 JWT 鉴权头和必填 `workspace_id`，已修复；复审确认示例可按文档口径调用，未发现新增误导或格式问题；无风险、无歧义、无差错。

---

### Task 9: 端到端回归验证

**处理的问题**

确认接入没有破坏原有 DeepDoc、上传、重处理、分块、trace 和前端链路。

**需要执行**

后端 targeted tests：

```powershell
pytest openrag/tests/test_parser_factory.py `
  openrag/tests/test_paddleocr_pdf_adapter.py `
  openrag/tests/test_paddleocr_pdf_integration.py `
  openrag/tests/parity/test_orchestration_parity.py `
  openrag/tests/test_chunk_engine.py `
  openrag/tests/test_document_processing_trace.py `
  openrag/tests/test_parse_profile_persistence.py -q
```

前端 tests：

```powershell
cd web
npm test -- --run
```

前端 build：

```powershell
cd web
npm run build
```

配置验证：

```powershell
docker compose -f docker/docker-compose.prod.yml config
kubectl apply --dry-run=client -f k8s/13-configmap-openrag-llm.yaml
kubectl apply --dry-run=client -f k8s/10-task-worker.yaml
```

**验收标准**

- 未选择 PaddleOCR 时，PDF 仍走 DeepDoc。
- 选择 `parser_type=paddleocr` 时，PDF 走 PaddleOCR adapter。
- PaddleOCR 失败时，当前文件任务失败并记录日志，不自动降级 DeepDoc。
- PaddleOCR 成功时，chunk 持久化包含 `page`、`bbox_x0`、`bbox_y0`、`bbox_x1`、`bbox_y1`、`source_block_id`、`source_char_start`、`source_char_end`。
- PDF chunk 点击回源可以跳转到对应页并高亮 block 级区域。
- 日志和 trace 能看到 `paddleocr.submit`、`paddleocr.poll`、`paddleocr.fetch_result`、`paddleocr.convert_blocks` 的耗时。
- 前端上传和重处理都能选择 PaddleOCR。
- Docker 外网 env 示例使用 `http://litellm.guozhijishu.com`。
- Kubernetes 内网 ConfigMap 使用 `http://litellm.dev.guozhijishu.com`。

**执行结果**

- 状态：已完成
- Coding：完成最终实现自检，确认 `auto`/`pdf` 仍走 DeepDoc/PDFParserAdapter，`parser_type=paddleocr` 独立路由到 PaddleOCR adapter，失败上抛且不自动降级，chunk 回源字段、日志/profile、前端选择、部署 URL 均有对应实现；未修改文件。
- Testing：后端 targeted tests 通过（`85 passed, 5 warnings`）；前端 `npm test` 通过（16 个测试文件、117 个测试），`npm run build` 通过；`docker compose -f docker/docker-compose.prod.yml config` 通过；`kubectl kustomize k8s` 通过；`kubectl apply --dry-run=client ...` 因本机无 Kubernetes API 连接 `localhost:8080` 失败，属于环境限制。计划中的 `npm test -- --run` 因当前脚本已包含 `vitest --run` 会重复传参，已用等价 `npm test` 验证。
- Review：最终审查确认默认 PDF 链路未被污染，PaddleOCR adapter 无硬编码内外网 URL 且不降级，日志/profile 不记录 OCR 全文或远端敏感错误原文，DocumentBlock/chunk 回源字段保留，前端/部署/文档均满足要求；无阻塞风险、无歧义、无差错。

## 冲突处理规则

- 如果发现必须修改 `PDFParserAdapter` 或 DeepDoc parser 才能接入 PaddleOCR，停止实现并说明原因。
- 如果发现 PaddleOCR 返回坐标不是 PDF 页面坐标，而是渲染图片像素坐标，需要先确认缩放换算方案；不能直接上线模糊坐标。
- 如果发现上传/重处理某个入口无法限制非 PDF 使用 PaddleOCR，停止并补充验证方案。
- 如果发现 `paddleocr` 不走 `pdf_manual` 会导致回源字段缺失，优先修正 `resolve_chunk_method()`，不改 chunk 引擎。
