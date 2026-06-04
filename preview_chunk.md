# OpenRag 外部 iframe 文档片段预览实现方案

## 1. 功能目标

外部接入方通过 OpenRag 的 search/research 接口获得文档片段后，用户点击某个 chunk，接入方系统在自己的页面中使用 iframe 打开 OpenRag 提供的预览页面。OpenRag 预览页负责展示目标文档并高亮命中的 chunk。

第一版范围：

- 用户点击 chunk 时再生成预览链接。
- 预览链接默认有效期 15 分钟。
- 只提供预览和 chunk 高亮，不显示下载按钮。
- 不实现外部用户审计字段。
- 不实现一次性 token。
- 不实现防截图、防复制、防浏览器开发者工具保存。
- 不把现有后台切块详情页直接暴露给外部 iframe。

## 2. 整体运转流程

```text
1. 外部系统后端调用 OpenRag search/research
   POST /service/v1/workspaces/{workspace_name}/search
   Header: X-OpenRag-Token

2. OpenRag 校验 service token 对 workspace 有 read 权限
   返回命中的 chunk 列表

3. 外部系统页面展示 search 返回的 chunk

4. 用户点击某个 chunk

5. 外部系统前端通知外部系统后端

6. 外部系统后端调用 OpenRag 创建预览链接
   POST /service/v1/workspaces/{workspace_name}/preview-links
   Header: X-OpenRag-Token

7. OpenRag 校验：
   - service token 对 workspace 有 read 权限
   - file 属于该 workspace
   - chunk 属于该 file
   - chunk 属于该 workspace

8. OpenRag 签发短期 preview token
   token 绑定 workspace_id、file_id、chunk_id、chunk_index、scope、exp

9. OpenRag 返回 preview_url
   https://openrag.guozhijishu.com/embed/document-preview#token=xxx

10. 外部系统前端把 preview_url 放入 iframe

11. iframe 中的 OpenRag 嵌入页读取 token

12. 嵌入页调用 OpenRag embed 只读 API
    加载文件预览内容和目标 chunk 定位信息

13. 嵌入页展示文档并高亮目标 chunk
```

## 3. 现有能力基础

当前 OpenRag 已有以下基础能力可复用：

- search 结果已返回定位字段：`file_id`、`chunk_id`、`chunk_index`、`page`、`bbox_x0`、`bbox_y0`、`bbox_x1`、`bbox_y1`、`source_char_start`、`source_char_end`。
- 内部切块详情页已有路由：`/workspaces/:workspaceId/files/:fileId/chunks?chunkId=...&chunkIndex=...`。
- 前端 `DocumentSourcePreview` 已支持 PDF bbox 高亮和文本 canonical source offset 高亮。
- 后端已有 workspace 文件相关接口：chunk 列表、原始文件内容、preview、chunk-source。
- 外部服务 API 已有 `X-OpenRag-Token` 鉴权模型。

第一版不直接复用完整内部切块详情页，因为该页面依赖 OpenRag 登录态和后台导航，不适合直接嵌入外部系统。第一版只复用其中的预览组件和后端读取逻辑。

## 4. 模块一：预览配置与接入边界

### 4.1 解决的问题

明确 OpenRag 对外生成预览链接时使用哪个公开域名、预览 token 默认多久过期，以及哪些外部系统域名允许 iframe 嵌入 OpenRag 预览页。

### 4.2 需要接入方提供的信息

- iframe 父页面的准确 origin，例如 `http://192.168.100.33:2026`。
- iframe 是否设置 `sandbox`。
- 如果设置 `sandbox`，第一版要求至少允许 `allow-scripts allow-same-origin`。

### 4.3 OpenRag 提供给接入方的信息

- 固定预览入口路径：`/embed/document-preview`。
- 完整预览链接由 OpenRag 创建预览链接接口返回，接入方不需要自行拼接。
- 默认有效期：15 分钟。
- iframe sandbox 推荐值：`allow-scripts allow-same-origin`。

### 4.4 配置项

```text
PREVIEW_PUBLIC_WEB_BASE_URL=https://openrag.guozhijishu.com
PREVIEW_TOKEN_TTL_SECONDS=900
PREVIEW_TOKEN_MAX_TTL_SECONDS=1800
PREVIEW_FRAME_ANCESTORS="'self' http://192.168.100.33:2026 http://192.168.100.32:2026 http://172.16.31.61:2026 http://172.16.31.156:2026"
```

说明：

- `PREVIEW_PUBLIC_WEB_BASE_URL`：OpenRag 后端生成 `preview_url` 时使用的公开 Web 根地址。
- 固定预览入口：OpenRag 前端路由 `/embed/document-preview`。
- `preview_url` 由 `PREVIEW_PUBLIC_WEB_BASE_URL + /embed/document-preview + #token=xxx` 组成。
- `PREVIEW_FRAME_ANCESTORS`：允许哪些外部页面把 OpenRag 预览页嵌入 iframe。

### 4.5 涉及文件

- `openrag/src/openrag/config.py`
- `docker/nginx/nginx.conf`
- `k8s/02-configmap-nginx.yaml`
- `k8s/12-ingress.yaml`
- `docker/docker-compose.prod.yml`
- `k8s/09-api.yaml`

### 4.6 需要实现的函数

```python
def get_preview_public_web_base_url() -> str:
    ...
```

功能：

- 从配置读取 `PREVIEW_PUBLIC_WEB_BASE_URL`。
- 去除尾部 `/`。
- 未配置时可回退到请求头推导的 origin，但生产建议显式配置。

```python
def get_preview_token_ttl_seconds() -> int:
    ...
```

功能：

- 从配置读取默认 TTL。
- 默认返回 900 秒。

```python
def get_preview_token_max_ttl_seconds() -> int:
    ...
```

功能：

- 从配置读取最大 TTL。
- 默认返回 1800 秒。

### 4.7 预期影响

- OpenRag 能生成接入方浏览器可访问的完整预览链接。
- iframe 预览页只允许被配置的外部系统域名嵌入。
- 预览链接不会被签成长链接，默认 15 分钟后失效。

### 4.8 验证方式

- 配置 `PREVIEW_PUBLIC_WEB_BASE_URL=https://openrag.guozhijishu.com` 后，创建预览链接接口返回该域名下的 URL。
- 未配置 `PREVIEW_TOKEN_TTL_SECONDS` 时，返回 `ttl_seconds=900`。
- 非白名单域名 iframe 加载被浏览器 CSP 拦截。
- 白名单域名 iframe 可以正常加载预览页。

## 5. 模块二：preview token 服务

### 5.1 解决的问题

iframe 中的浏览器没有 OpenRag 登录态，也不能持有长期 `X-OpenRag-Token`。preview token 用于把一次合法的外部 search 结果转换成一个短期、只读、绑定单个文档 chunk 的浏览器预览凭证。

### 5.2 需要接入方提供的信息

无需额外提供信息。接入方只需要在创建预览链接时传入 search 返回的 `file_id`、`chunk_id`、可选 `chunk_index`。

### 5.3 实现功能

- 签发短期 JWT preview token。
- token 绑定 `workspace_id`、`file_id`、`chunk_id`、`chunk_index`。
- token 绑定 `scope=document_preview`。
- token 过期后不能继续访问。
- token 不能用于 search、upload、delete、download。
- 后端只信 token 中签名绑定的定位信息，不信浏览器 URL 参数中的 workspace/file/chunk。

### 5.4 token claims

```json
{
  "scope": "document_preview",
  "workspace_id": 7,
  "file_id": 9,
  "chunk_id": "chunk-42",
  "chunk_index": 42,
  "iat": 1780481700,
  "exp": 1780482600
}
```

### 5.5 涉及文件

- 新增 `openrag/src/openrag/services/preview_token_service.py`
- 可能复用 `openrag/src/openrag/security.py`
- 可能复用 `openrag/src/openrag/config.py`

### 5.6 需要实现的函数

```python
class PreviewTokenClaims(BaseModel):
    scope: str
    workspace_id: int
    file_id: int
    chunk_id: str
    chunk_index: int | None = None
    iat: int
    exp: int
```

功能：

- 表示 preview token 解码后的受限访问范围。

```python
def clamp_preview_ttl(ttl_seconds: int | None) -> int:
    ...
```

功能：

- 未传时返回默认 900 秒。
- 传入值小于最小值时使用最小值。
- 传入值大于最大值时使用最大值。

```python
def create_preview_token(
    *,
    workspace_id: int,
    file_id: int,
    chunk_id: str,
    chunk_index: int | None,
    ttl_seconds: int | None,
) -> tuple[str, datetime]:
    ...
```

功能：

- 生成 `scope=document_preview` 的 JWT。
- 写入 `workspace_id`、`file_id`、`chunk_id`、`chunk_index`。
- 写入 `iat` 和 `exp`。
- 返回 token 和过期时间。

实现方式：

- 使用 OpenRag 现有 `SECRET_KEY` 和 `HS256` 签名。
- TTL 使用 `clamp_preview_ttl` 处理。

```python
def decode_preview_token(token: str) -> PreviewTokenClaims:
    ...
```

功能：

- 校验 token 签名。
- 校验 token 未过期。
- 校验 `scope == "document_preview"`。
- 返回 `PreviewTokenClaims`。

### 5.7 预期影响

- 预览链接被转发后，在有效期内最多只能预览 token 绑定的同一个文档 chunk。
- 攻击者无法通过修改 URL 参数访问其他 workspace/file/chunk。
- 长期 service token 不会暴露给浏览器。

### 5.8 验证方式

- 有效 token 能解码出 workspace/file/chunk。
- 过期 token 被拒绝。
- 篡改 token 内容后签名校验失败。
- scope 错误的 token 被拒绝。
- TTL 默认值为 900 秒。
- TTL 超过最大值时被限制到最大值。

## 6. 模块三：创建预览链接的 service API

### 6.1 解决的问题

接入方用户点击 chunk 后，需要由接入方后端携带长期 `X-OpenRag-Token` 向 OpenRag 换取一个短期 `preview_url`。

### 6.2 需要接入方提供的信息

路径参数：

- `workspace_name`

请求头：

- `X-OpenRag-Token`

请求体：

- `file_id`
- `chunk_id`
- `chunk_index`，可选但建议传入
- `ttl_seconds`，可选，不传默认 900 秒

### 6.3 OpenRag 提供给接入方的信息

- `preview_url`
- `expires_at`
- `ttl_seconds`

### 6.4 新增接口

```http
POST /service/v1/workspaces/{workspace_name}/preview-links
X-OpenRag-Token: sk-...
Content-Type: application/json
```

请求体：

```json
{
  "file_id": 9,
  "chunk_id": "chunk-42",
  "chunk_index": 42,
  "ttl_seconds": 900
}
```

响应：

```json
{
  "preview_url": "https://openrag.guozhijishu.com/embed/document-preview#token=xxx",
  "expires_at": "2026-06-03T18:30:00+08:00",
  "ttl_seconds": 900
}
```

错误：

- `401`：service token 缺失或无效。
- `403`：service token 没有该 workspace 的 read 权限。
- `404`：workspace、file 或 chunk 不存在。
- `400`：file 是目录，或 `chunk_index` 与 `chunk_id` 不一致。

### 6.5 涉及文件

- `openrag/src/openrag/api/service_api.py`
- `openrag/src/openrag/services/preview_token_service.py`
- `openrag/tests/test_service_api.py`

### 6.6 需要实现的函数

```python
class ServicePreviewLinkRequest(BaseModel):
    file_id: int
    chunk_id: str
    chunk_index: int | None = None
    ttl_seconds: int | None = None
```

功能：

- 表示接入方创建预览链接的请求体。

```python
class ServicePreviewLinkResponse(BaseModel):
    preview_url: str
    expires_at: datetime
    ttl_seconds: int
```

功能：

- 表示 OpenRag 返回给接入方的预览链接信息。

```python
def resolve_preview_target(
    db: Session,
    *,
    workspace_id: int,
    file_id: int,
    chunk_id: str,
    chunk_index: int | None,
) -> tuple[FileModel, DocumentChunk]:
    ...
```

功能：

- 查询 file。
- 校验 file 属于 workspace。
- 校验 file 不是目录。
- 查询 chunk。
- 校验 chunk 属于 file。
- 校验 chunk 属于 workspace。
- 如果传入 `chunk_index`，校验它和数据库中的 chunk index 一致。

```python
def build_preview_url(*, base_url: str, token: str) -> str:
    ...
```

功能：

- 拼接完整 iframe 预览链接。
- 格式为 `{base_url}/embed/document-preview#token={token}`。

```python
async def service_create_preview_link(...):
    ...
```

功能：

- FastAPI 接口处理函数。
- 根据 `workspace_name` 查 workspace。
- 校验 `X-OpenRag-Token` 对 workspace 有 read 权限。
- 调用 `resolve_preview_target`。
- 调用 `create_preview_token`。
- 返回 `preview_url`、`expires_at`、`ttl_seconds`。

### 6.7 预期影响

- search 接口不需要提前给所有结果生成 token。
- 用户点击时才生成预览链接，减少 token 过期导致的体验问题。
- service token 仍只存在于接入方后端，不进入浏览器。

### 6.8 验证方式

- 使用有 read 权限的 service token 可以成功创建 preview link。
- 使用无权限 service token 返回 403。
- 传入不存在的 file 返回 404。
- 传入不属于该 workspace 的 file 返回 404 或 403。
- 传入不属于该 file 的 chunk 返回 404。
- 传入错误 `chunk_index` 返回 400。
- 成功响应中的 `preview_url` 使用 `https://openrag.guozhijishu.com`。
- 成功响应中的 `ttl_seconds` 默认为 900。

## 7. 模块四：embed 专用只读后端 API

### 7.1 解决的问题

iframe 页面需要加载文件元信息、目标 chunk、文件内容、后端预览内容或 canonical chunk source。但 iframe 页面不能使用 OpenRag 登录 JWT，也不能使用长期 service token，因此需要一组只接受 preview token 的只读 API。

### 7.2 需要接入方提供的信息

接入方无需直接调用这些 API。它只需要把 OpenRag 返回的 `preview_url` 放入 iframe。

### 7.3 OpenRag 提供给接入方的信息

这些接口不直接作为接入方契约暴露。它们是 OpenRag iframe 页面内部使用的接口。

### 7.4 新增接口

#### 7.4.1 获取预览上下文

```http
GET /embed/v1/document-preview
X-OpenRag-Preview-Token: <preview_token>
```

响应：

```json
{
  "file": {
    "id": 9,
    "workspace_id": 7,
    "name": "制度说明.pdf",
    "uri": "/docs/制度说明.pdf",
    "mime_type": "application/pdf",
    "simple_status": "done"
  },
  "chunk": {
    "file_id": 9,
    "workspace_id": 7,
    "filename": "制度说明.pdf",
    "chunk_id": "chunk-42",
    "chunk_index": 42,
    "text": "命中的片段...",
    "is_truncated": false,
    "page": 3,
    "bbox_x0": 10,
    "bbox_y0": 20,
    "bbox_x1": 300,
    "bbox_y1": 80,
    "source_char_start": 1200,
    "source_char_end": 1350,
    "position_int": [],
    "positions": []
  },
  "expires_at": "2026-06-03T18:30:00+08:00"
}
```

#### 7.4.2 获取文件内容流

```http
GET /embed/v1/files/content
X-OpenRag-Preview-Token: <preview_token>
```

响应：

- 文件流。
- 用于 PDF、图片、原始文本等预览。
- 响应头使用 `Content-Disposition: inline`。
- 不提供下载按钮，不提供 attachment 下载语义。

#### 7.4.3 获取后端转换预览

```http
GET /embed/v1/files/preview
X-OpenRag-Preview-Token: <preview_token>
```

响应：

```json
{
  "format": "html",
  "content": "<div>...</div>"
}
```

用于 Office 等需要后端转换为 HTML/text 的文件。

#### 7.4.4 获取 canonical chunk source

```http
GET /embed/v1/files/chunk-source
X-OpenRag-Preview-Token: <preview_token>
```

响应：

```json
{
  "format": "text",
  "content": "canonical chunk source..."
}
```

用于 MD/TXT/DOCX 按 `source_char_start`、`source_char_end` 高亮。

### 7.5 涉及文件

- 新增 `openrag/src/openrag/api/embed_preview_api.py`
- `openrag/src/openrag/api/main.py`
- `openrag/src/openrag/services/preview_token_service.py`
- 复用 `openrag/src/openrag/api/workspace_file_api.py` 中的读取逻辑
- 复用 `openrag/src/openrag/services/file_preview.py`
- 新增 `openrag/tests/test_embed_preview_api.py`

### 7.6 需要实现的函数

```python
def get_preview_claims_from_header(
    x_openrag_preview_token: str | None,
) -> PreviewTokenClaims:
    ...
```

功能：

- 从 `X-OpenRag-Preview-Token` 读取 preview token。
- 调用 `decode_preview_token`。
- 缺失或无效时返回 401。

```python
def resolve_claims_file_and_chunk(
    db: Session,
    claims: PreviewTokenClaims,
) -> tuple[FileModel, DocumentChunk]:
    ...
```

功能：

- 使用 token claims 中的 workspace/file/chunk 查询数据库。
- 校验 file/chunk 关系。
- 校验 chunk_index。
- 所有定位信息均来自 token，不来自 URL 参数。

```python
def get_embed_document_preview(...):
    ...
```

功能：

- 返回文件元信息和目标 chunk 定位信息。

```python
def get_embed_file_content(...):
    ...
```

功能：

- 根据 token 绑定的 file 从对象存储读取文件流。
- 返回 StreamingResponse。
- 使用 `inline` 响应头。

```python
def get_embed_file_preview(...):
    ...
```

功能：

- 读取 token 绑定的文件 bytes。
- 调用 `build_file_preview` 转换为 HTML/text。
- 返回 JSON。

```python
def get_embed_chunk_source(...):
    ...
```

功能：

- 查询 token 绑定文件的最新 completed parse artifact。
- 读取 canonical markdown。
- 返回 `{format: "text", content: "..."}`。

### 7.7 预期影响

- iframe 页面不依赖 OpenRag 登录态。
- preview token 只允许读取绑定文档的预览所需内容。
- 不暴露目录列表、搜索、上传、删除、下载能力。

### 7.8 验证方式

- 不带 token 调用 embed API 返回 401。
- 过期 token 返回 401。
- 篡改 token 返回 401。
- token 绑定 file 不存在返回 404。
- token 绑定 chunk 不存在返回 404。
- PDF 文件可通过 content 接口加载。
- Office 文件可通过 preview 接口加载。
- MD/TXT/DOCX 可通过 chunk-source 接口加载并高亮。

## 8. 模块五：iframe 前端预览页

### 8.1 解决的问题

提供一个适合外部系统 iframe 嵌入的轻量 OpenRag 页面。该页面不显示后台导航，不要求 OpenRag 登录，只展示文档预览和命中 chunk 高亮。

### 8.2 需要接入方提供的信息

接入方只需要使用 OpenRag 返回的 `preview_url`：

```html
<iframe
  src="https://openrag.guozhijishu.com/embed/document-preview#token=xxx"
  sandbox="allow-scripts allow-same-origin"
></iframe>
```

### 8.3 OpenRag 提供给接入方的信息

- `preview_url`。
- iframe sandbox 推荐配置。
- 链接过期后的错误展示由 OpenRag 嵌入页处理。

### 8.4 涉及文件

- `web/src/App.tsx`
- 新增 `web/src/pages/EmbeddedDocumentPreview.tsx`
- 新增 `web/src/pages/EmbeddedDocumentPreview.css`
- 新增 `web/src/services/embedPreviewApi.ts`
- 修改 `web/src/components/document-source-preview/index.tsx`
- 新增 `web/src/pages/EmbeddedDocumentPreview.test.tsx`

### 8.5 需要实现的函数

```typescript
function readPreviewTokenFromHash(): string | null
```

功能：

- 从 `window.location.hash` 中读取 `token`。
- token 放在 fragment 中，避免作为 query 进入常规访问日志。

```typescript
const embedPreviewAPI = {
  getContext(token: string): Promise<EmbedDocumentPreviewResponse>
  fetchContentBlob(token: string): Promise<Blob>
  fetchPreview(token: string): Promise<{ format: 'html' | 'text'; content: string }>
  fetchChunkSource(token: string): Promise<{ format: 'text'; content: string }>
}
```

功能：

- 调用 `/api/embed/v1/**` 接口。
- 每个请求都带 `X-OpenRag-Preview-Token`。
- 不使用 localStorage 中的 OpenRag JWT。
- 401/403 时不跳转登录页。

```tsx
function EmbeddedDocumentPreview()
```

功能：

- 读取 preview token。
- 调用 `embedPreviewAPI.getContext` 获取 file/chunk。
- 将 file/chunk 转成 `DocumentSourcePreview` 可用的数据结构。
- 传入 embed 专用 fetchers。
- 显示预览和高亮。
- token 缺失、过期或无效时显示错误提示。

对 `DocumentSourcePreview` 的改动：

```typescript
type DocumentSourcePreviewFetchers = {
  fetchContentBlob(fileId: number, workspaceId?: number): Promise<Blob>
  fetchPreview(fileId: number, workspaceId?: number): Promise<{ format: 'html' | 'text'; content: string }>
  fetchChunkSource(workspaceId: number, fileId: number): Promise<{ format: 'text'; content: string }>
}
```

功能：

- 默认仍使用现有 `filesAPI`。
- embed 页面传入 `embedPreviewAPI` 对应 fetcher。
- 复用 PDF bbox 高亮、文本 offset 高亮逻辑。

### 8.6 预期影响

- OpenRag 内部页面不受影响。
- 外部 iframe 页面无后台导航、无登录跳转、无下载按钮。
- 预览体验复用现有组件，减少重复实现。

### 8.7 验证方式

- 访问 `/embed/document-preview#token=有效token` 能正常显示文档预览。
- 不带 token 显示错误。
- 过期 token 显示“预览链接已过期”。
- PDF chunk 自动滚动到目标页并高亮。
- MD/TXT/DOCX chunk 使用 canonical source 高亮。
- 页面不出现下载按钮。
- embed API 401 不触发全局登录跳转。

## 9. 模块六：iframe 安全头与跨域

### 9.1 解决的问题

允许指定外部系统 iframe 嵌入 OpenRag 预览页，同时避免任意网站嵌入。

### 9.2 需要接入方提供的信息

- iframe 父页面 origin，例如 `http://192.168.100.33:2026`。
- 是否配置 iframe sandbox。

### 9.3 实现功能

对 OpenRag Web 响应设置：

```http
Content-Security-Policy: frame-ancestors 'self' http://192.168.100.33:2026 http://192.168.100.32:2026 http://172.16.31.61:2026 http://172.16.31.156:2026
Referrer-Policy: no-referrer
```

不设置：

```http
X-Frame-Options: DENY
X-Frame-Options: SAMEORIGIN
```

说明：

- `frame-ancestors` 控制哪些父页面可以 iframe 嵌入 OpenRag。
- `Referrer-Policy: no-referrer` 降低预览链接泄漏到第三方请求 Referer 的风险。
- iframe 页面和 API 同源时，不需要给外部前端开放 API CORS。
- 外部系统后端调用 OpenRag service API 不受浏览器 CORS 影响。

### 9.4 涉及文件

- `docker/nginx/nginx.conf`
- `k8s/02-configmap-nginx.yaml`
- `k8s/12-ingress.yaml`

### 9.5 需要实现的函数

如果在 FastAPI 层实现响应头，可新增中间件：

```python
async def preview_security_headers_middleware(request, call_next):
    ...
```

功能：

- 对 `/embed/document-preview` 或所有 Web 响应添加 CSP。
- 对响应添加 `Referrer-Policy`。

如果在 Nginx/Ingress 层实现，则不需要 Python 函数，通过网关配置完成。

### 9.6 预期影响

- 白名单外部系统可以 iframe 打开预览页。
- 非白名单站点无法 iframe 嵌入。
- 不扩大 OpenRag API CORS 暴露范围。

### 9.7 验证方式

- 白名单域名 iframe 正常加载。
- 非白名单域名 iframe 被浏览器拦截。
- 浏览器网络请求中不出现 `X-OpenRag-Token`。
- iframe 中只出现短期 `X-OpenRag-Preview-Token`。

## 10. 模块七：测试与文档

### 10.1 解决的问题

验证 preview link、preview token、embed API、iframe 页面、安全边界不会回归，并向接入方说明正确调用方式。

### 10.2 涉及文件

- 新增 `openrag/tests/test_preview_token_service.py`
- 修改 `openrag/tests/test_service_api.py`
- 新增 `openrag/tests/test_embed_preview_api.py`
- 新增 `web/src/pages/EmbeddedDocumentPreview.test.tsx`
- 修改 `web/src/services/api.test.ts` 或新增 `web/src/services/embedPreviewApi.test.ts`
- 更新 `docs/04-外部系统接入与API.md`

### 10.3 测试内容

后端：

- 有 read 权限的 service token 能创建 preview link。
- 无权限 service token 不能创建 preview link。
- file 不属于 workspace 时拒绝。
- chunk 不属于 file 时拒绝。
- chunk_index 不一致时拒绝。
- preview token 默认 TTL 为 900 秒。
- preview token 过期后 embed API 拒绝访问。
- preview token 不能访问其他 file/chunk。
- embed content/preview/chunk-source 都只读。

前端：

- 有效 token 正常渲染预览页。
- 缺失 token 显示错误。
- 过期 token 显示过期提示。
- embed API 返回 401 时不跳登录页。
- 页面不显示下载按钮。
- PDF/text chunk 高亮路径正常。

文档：

- 说明 search 后点击时生成 preview link。
- 说明创建 preview link 的请求体和响应。
- 说明 iframe 示例代码。
- 说明 token 默认 15 分钟过期。
- 说明只提供预览，不提供下载按钮。
- 说明接入方不要把 `X-OpenRag-Token` 放到前端。

### 10.4 预期影响

- 接入方可以按文档完成 iframe 预览集成。
- 权限边界和 token 行为有自动化测试覆盖。

### 10.5 验证方式

后端：

```bash
python -m pytest openrag/tests/test_preview_token_service.py openrag/tests/test_service_api.py openrag/tests/test_embed_preview_api.py -q
```

前端：

```bash
cd web
npm test -- --run src/pages/EmbeddedDocumentPreview.test.tsx src/services/embedPreviewApi.test.ts
```

## 11. 接入方调用示例

### 11.1 search

```http
POST /service/v1/workspaces/MyWorkspace/search
X-OpenRag-Token: sk-...
Content-Type: application/json
```

请求体：

```json
{
  "query": "制度有效期",
  "top_k": 10
}
```

响应片段：

```json
{
  "results": [
    {
      "text": "命中的文档片段...",
      "score": 0.86,
      "file_id": 9,
      "chunk_id": "chunk-42",
      "chunk_index": 42,
      "filename": "制度说明.pdf",
      "page": 3,
      "bbox_x0": 10,
      "bbox_y0": 20,
      "bbox_x1": 300,
      "bbox_y1": 80,
      "source_char_start": 1200,
      "source_char_end": 1350
    }
  ]
}
```

### 11.2 用户点击 chunk 后创建预览链接

```http
POST /service/v1/workspaces/MyWorkspace/preview-links
X-OpenRag-Token: sk-...
Content-Type: application/json
```

请求体：

```json
{
  "file_id": 9,
  "chunk_id": "chunk-42",
  "chunk_index": 42
}
```

响应：

```json
{
  "preview_url": "https://openrag.guozhijishu.com/embed/document-preview#token=xxx",
  "expires_at": "2026-06-03T18:30:00+08:00",
  "ttl_seconds": 900
}
```

### 11.3 iframe 打开预览

```html
<iframe
  src="https://openrag.guozhijishu.com/embed/document-preview#token=xxx"
  sandbox="allow-scripts allow-same-origin"
  style="width: 100%; height: 80vh; border: 0"
></iframe>
```

## 12. 安全边界说明

- `X-OpenRag-Token` 只允许放在接入方后端。
- 浏览器 iframe 只使用短期 preview token。
- preview token 是 bearer token，被转发后，在有效期内可打开同一个文档 chunk。
- preview token 不能用于访问其他 workspace/file/chunk。
- preview token 不能用于 search、upload、delete。
- 第一版不显示下载按钮，但不承诺阻止截图、复制或通过浏览器开发者工具保存预览内容。
- 生产环境建议必须使用 HTTPS。
