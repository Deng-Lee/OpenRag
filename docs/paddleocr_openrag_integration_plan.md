# PaddleOCR 作为默认 PDF 解析器的接入方案

## 最终公开契约

- `auto`：非 PDF 保持既有自动识别；PDF 实际使用 PaddleOCR，并将文件的 `parser_type` 持久化为 `pdf`。
- `pdf`：显式使用 PaddleOCR，仅接受 PDF，并在入库或替换前校验 `%PDF-` 魔数。
- `deepdoc`：显式使用原有 DeepDoc PDF 解析器，仅接受 PDF。
- `pdf`、`deepdoc` 都使用 `pdf_manual` 分块。
- 旧公开值 `paddleocr` 直接移除，不提供别名或兼容转换。
- PaddleOCR 失败时当前任务失败，不自动降级到 DeepDoc。

内部仍保留 PaddleOCR 技术标识，例如 adapter 文件名、远端 `/paddleocr/...` 路由、日志 stage、`source="paddleocr"` 与 block id；这些不是公开 `parser_type`。

## 后端改动

- `ParserFactory` 的 `.pdf` 默认映射和公开 `pdf` 映射指向 `PaddleOCRPDFParserAdapter`。
- 新增公开 `deepdoc` 类型，并将其直连 `PDFParserAdapter`。
- 上传、替换、upsert、重处理和 worker 处理统一应用 PDF 默认选择规则。
- 仅将 `auto + PDF` 解析为 `pdf`；例如 `auto + report.docx` 仍保持 `auto`。
- 所有公开入口拒绝 `parser_type=paddleocr`。

## 前端改动

- 解析器选项显示为：
  - `自动检测（PDF 默认使用 PaddleOCR）`
  - `PDF（PaddleOCR 解析）`
  - `PDF（DeepDoc 解析）`
- 重处理时选择 `auto` 必须显式发送 `auto`，由后端重新计算 PDF 默认解析器。

## 配置与发布

- PaddleOCR 服务仍由 worker 的 `PADDLEOCR_SERVER_URL` 指定，不新增默认解析器开关。
- 前后端应协同发布；旧前端发送 `paddleocr` 到新后端会收到 400。
- 若环境中已经产生 `files.parser_type='paddleocr'` 的数据，应在发布前一次性改为 `pdf`；不要批量修改空值或 `auto` 的历史 PDF，避免误标旧 DeepDoc 结果。

## 验收标准

- `auto + PDF`、`pdf + PDF` 都选择 PaddleOCR，且有效 `parser_type` 为 `pdf`。
- `deepdoc + PDF` 选择 `PDFParserAdapter`。
- `paddleocr` 被请求校验与工厂路由拒绝。
- `auto + DOCX` 仍为 `auto`，其他格式行为不变。
- PaddleOCR PDF 魔数校验、`pdf_manual` 路由、前端选项和重处理请求均有自动化测试覆盖。
