# OpenRag 外部系统接入与 API

> 与 [01-项目说明.md](./01-项目说明.md)、[02-Kubernetes部署.md](./02-Kubernetes部署.md)、[03-使用说明.md](./03-使用说明.md) 为同一套四份说明文档。

本文分两部分：**接入流程与用户 JWT 类 API**；**服务令牌 `/service/v1` 的鉴权、路径、参数与示例**。

---

## 第一部分：接入总览与用户 JWT

### 1.1 集成方式总览

| 场景 | 鉴权 | 典型用途 |
|------|------|----------|
| 浏览器 / 人类用户 | `Authorization: Bearer <JWT>` | 管理后台、日常检索与上传 |
| 业务系统 / 脚本 / 微服务 | `X-OpenRag-Token: sk-...` | 目录同步、流水线写库、无人值守检索 |

两类凭证**不要混用**：服务路由 `/service/v1/**` 只认 `X-OpenRag-Token`；用户路由使用 OAuth2 Bearer JWT。

### 1.2 推荐接入流程（服务令牌）

```mermaid
flowchart LR
  A[系统管理员登录 Web] --> B[进入 服务令牌]
  B --> C[创建令牌]
  C --> D[绑定多个工作区并设置权限]
  D --> E[安全下发 sk-... 至外部系统]
  E --> F[HTTPS 调用 /service/v1]
```

1. **管理员**在 Web 端打开 **服务令牌**（`/service-tokens`），创建令牌并授权绑定一个或多个工作区（每个工作区独立设置 `read` 或 `write` 权限）。
2. 记录返回的完整密钥（`sk-` 前缀）；创建响应会返回明文，后续令牌创建者或管理员也可通过管理端点查看完整密钥。
3. 外部系统保存密钥于 **Secret 管理**（环境变量、Vault、K8s Secret），禁止写入前端或版本库。
4. 调用时统一设置请求头：`X-OpenRag-Token: <完整密钥>`。
5. URL 中的工作区标识为工作区的 **`name`（全局唯一）**，含中文或空格时需 **URL 编码**；令牌须已绑定该工作区，否则 **403**。

### 1.3 用户 JWT API（管理 / 人机）

面向交互式与「带用户身份」的集成，可直接使用 FastAPI 暴露的路由（前缀均相对于 API 根，无 `/api`；若经网关挂载 `/api`，请自行剥离前缀）。

**获取令牌**：`POST /users/login`、注册 `POST /users/register` 等——见 `/docs` 中 **users** 标签。

**常用前缀**（摘录，以 OpenAPI 为准）：

| 前缀 | 说明 |
|------|------|
| `/users` | 注册、登录、当前用户信息、用户角色 |
| `/teams` | 团队 CRUD 与团队成员管理 |
| `/workspaces` | 工作区 CRUD、成员 |
| `/files` | 文件上传、目录创建、列表筛选、内容/预览、移动、删除、重处理、文件级权限 |
| `/workspaces/{workspace_id}/files` | 强工作区身份的文件 chunks、chunk source、内容与预览读取 |
| `/search` | 语义检索、分层检索、chunk 上下文 |
| `/workspaces/{workspace_id}/tasks`、`/broker` | 工作区任务查询/重试/取消与 worker 调度运维 |
| `/share` | 文件分享链接创建、列表、访问与吊销 |
| `/roles` | 角色与授权（管理用） |
| `/traces`、`/eval` | 文档处理 / 检索可观测与检索质量评测 |
| `/service-tokens` | 服务令牌管理（CRUD、绑定、吊销）——见第二部分 |
| `/embed/v1` | iframe 文档预览内部只读 API，仅配合短期 preview token 使用 |

JWT 文件上传 `POST /files/upload` 还支持 `document_type`（`general`、`manual`、`laws`）和可选 `tag`，其中 `tag` 是工作区内唯一的单文件业务标识，重复时返回 **409**；`POST /files/{file_id}/reprocess` 可在重处理时更新或保留 `document_type`。服务令牌上传接口暴露 `parser_type` 和可选 `tag`，新文件默认按 `general` 处理。

文件删除接口默认采用异步软删除：被删除文件会立即从列表、检索、预览、分享与 service-token 读取接口中隐藏，并释放 `tag`；后台任务随后清理对象存储、chunk 与向量数据。

完整契约：**部署后打开** `https://<api-host>/docs` 或 `https://<domain>/api/docs`（若使用 Nginx `/api` 代理）。

### 1.4 网络、TLS 与限流

- 生产环境应对公网 **HTTPS** 终止（Ingress / 网关），后端可只接收集群内 HTTP。
- 上传体积当前由后端 `MAX_FILE_SIZE` 约束为 **100 MB**。
- 对 `/service/v1` 建议在网关侧做 **IP allowlist**、**速率限制** 与密钥轮换。

### 1.5 与前端同源部署时的基地址

若 Nginx 将浏览器请求 `/api/*` 转发到 FastAPI，则：

- 浏览器可配置前端 `VITE_API_URL=/api`，请求路径为 `/api/users/...` 等；
- 外部服务端若在集群外，通常使用 **直连 API** 的绝对地址，是否带 `/api` 取决于网关规则。

---

## 第二部分：`/service/v1` 服务令牌 API

> 本部分面向需要通过 Service Token 鉴权接入 OpenRag 的外部系统、脚本与微服务。内容自包含，无需了解 OpenRag 内部架构或用户 JWT 鉴权体系。

### 2.1 快速开始

**三步接入：**

1. **获取令牌** — 系统管理员在 OpenRag Web 端「服务令牌」页面创建令牌，并授权绑定一个或多个工作区（每个工作区独立设置 `read` 或 `write` 权限）。
2. **保存密钥** — 将完整密钥字符串（`sk-...`）存入 Secret 管理工具（环境变量、Vault、K8s Secret），禁止写入前端代码或版本库。
3. **调用接口** — 所有请求携带 `X-OpenRag-Token` 头即可访问 `/service/v1` 下的 14 个机读接口。

```bash
# 示例：列根目录树
curl -H "X-OpenRag-Token: sk-your-secret-key" \
  "https://api.example.com/service/v1/workspaces/MyWorkspace/tree?path_prefix=/"

# 示例：查询令牌可访问的工作空间
curl -H "X-OpenRag-Token: sk-your-secret-key" \
  "https://api.example.com/service/v1/workspaces"
```

### 2.2 鉴权机制

#### 2.2.1 认证头

所有 `/service/v1/**` 接口使用自定义 HTTP 头：

```http
X-OpenRag-Token: sk-<完整密钥字符串>
```

**关键约定：**

- 使用 **`X-OpenRag-Token`** 头，**不要**放在 `Authorization: Bearer` 里（那是用户 JWT，两类凭证互不兼容）。
- 密钥格式：以 `sk-` 开头，后接 43 字符 Base64 随机串（总长 46 字符）。
- 前后空格会被自动 trim；无效、已吊销或格式错误的密钥 → **401**。

#### 2.2.2 令牌属性

| 属性 | 说明 |
|------|------|
| **绑定工作区** | 令牌可授权绑定多个工作区，每个工作区拥有独立权限等级。可通过「管理绑定」接口增删改绑定。调用单工作区 `/workspaces/{workspace_name}` 接口时，令牌须已绑定该工作区，否则 **403**；调用多工作区检索时，无权限工作区会进入 `skipped_workspaces`。 |
| **权限等级** | 每个绑定独立设置 `read`（只读）或 `write`（读写）。`write` 包含 `read`。可在「管理绑定」接口修改。 |
| **吊销** | 吊销后立即对所有 `/service/v1` 接口失效。 |

#### 2.2.3 权限矩阵

| 操作 | 所需令牌权限 |
|------|----------------|
| 目录树、列子项、按前缀查询、文件元数据、按 tag 查询、语义检索、按文件名搜索、创建预览链接 | **read** 或 **write** |
| 上传新文件、按 tag 幂等写入、覆盖已有文件、删除文件 | **write** |

只读令牌调用写接口 → **403**，`detail`：`Token permission insufficient`。
多工作区检索中，`write` 绑定同样视为具备读取权限。

### 2.3 前置概念

| 概念 | 说明 |
|------|------|
| **工作区标识（URL）** | 路径中使用工作区的 **`name`**（全局唯一展示名），不是数字 `id`。含中文或空格时需 **URL 编码**。 |
| **逻辑路径** | 文件在工作区内的路径，**必须以 `/` 开头**（如 `/`、`/docs/report.pdf`）。禁止 `..` 穿越，检测到 → **400**。 |
| **文件 tag** | 可选的单文件业务标识，工作区内唯一；允许字符为 `A-Z`、`a-z`、`0-9`、`.`、`_`、`:`、`-`，长度 1-128。用于外部系统按业务主键查询或幂等写入文件。 |
| **软删除** | 默认异步删除会先设置 `deleted_at` 并清空 `tag`，使文件立即从读取接口隐藏并释放 tag；物理清理由后台任务完成。在清理完成前，原路径仍可能被视为“正在删除中”。 |
| **处理状态** | 文件上传后自动进入解析流水线，`processing_status` 可能值为 `pending` → `parsing` → `building_hierarchy` → `embedding` → `completed`（或 `failed`）。 |
| **基地址** | 路由前缀 `/service/v1`。若经反向代理加 `/api` 前缀，完整路径为 `/api/service/v1/...`，以实际部署为准。 |

### 2.4 接口一览

以下路径均相对于 **`/service/v1`**，且均须携带 **`X-OpenRag-Token`** 头。

**管理端点**（JWT 鉴权，不在 `/service/v1` 下）：

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/service-tokens` | 创建令牌（含 `workspaces` 绑定数组） |
| `GET` | `/service-tokens` | 列出令牌（可选 `workspace_id` 过滤） |
| `PATCH` | `/service-tokens/{token_id}/workspaces` | 管理绑定（add/update/remove） |
| `DELETE` | `/service-tokens/{token_id}` | 吊销令牌 |
| `GET` | `/service-tokens/{token_id}/secret` | 查看完整密钥 |

**服务端点**：

| 方法 | 路径 | 说明 | 所需权限 |
|------|------|------|----------|
| `GET` | `/workspaces` | 查询令牌可访问的工作空间 | read |
| `GET` | `/workspaces/{workspace_name}/tree` | 嵌套目录树 | read |
| `GET` | `/workspaces/{workspace_name}/children` | 某目录一级子项 | read |
| `GET` | `/workspaces/{workspace_name}/entries/by-prefix` | 按前缀扁平列表 | read |
| `GET` | `/workspaces/{workspace_name}/documents/by-path` | 按路径取文件元数据 | read |
| `GET` | `/workspaces/{workspace_name}/documents/by-tag` | 按工作区内唯一 tag 取文件元数据 | read |
| `POST` | `/workspaces/{workspace_name}/documents` | 上传新文件（multipart） | write |
| `PUT` | `/workspaces/{workspace_name}/documents/upsert-by-tag` | 按 tag 幂等创建、更新或移动并替换文件 | write |
| `PUT` | `/workspaces/{workspace_name}/documents/by-path` | 覆盖已有文件 | write |
| `DELETE` | `/workspaces/{workspace_name}/documents/by-path` | 按路径删除文件，默认异步软删除 | write |
| `POST` | `/workspaces/{workspace_name}/search` | 语义检索（JSON body） | read |
| `POST` | `/workspaces/multi_space/search` | 多工作区语义检索（JSON body） | read |
| `GET` | `/workspaces/{workspace_name}/documents/search-by-name` | 按文件名子串模糊搜索 | read |
| `POST` | `/workspaces/{workspace_name}/preview-links` | 创建文档片段 iframe 预览链接 | read |

**外部系统可用能力汇总：**

| 能力 | 对应端点 | 说明 |
|------|----------|------|
| 工作区发现 | `GET /workspaces` | 查询当前服务令牌可访问的工作区、权限和基础信息 |
| 目录浏览 | `GET /workspaces/{workspace_name}/tree`、`children`、`entries/by-prefix` | 获取嵌套目录树、一级子项或按前缀展开的扁平列表 |
| 文件元数据查询 | `GET /workspaces/{workspace_name}/documents/by-path`、`documents/by-tag`、`documents/search-by-name` | 按逻辑路径、业务 tag 精确查询文件，或按文件名子串搜索 |
| 文件写入 | `POST /workspaces/{workspace_name}/documents`、`PUT /workspaces/{workspace_name}/documents/upsert-by-tag`、`PUT /workspaces/{workspace_name}/documents/by-path` | 上传新文件、按 tag 幂等写入或覆盖已有文件，需要 `write` 权限 |
| 文件删除 | `DELETE /workspaces/{workspace_name}/documents/by-path` | 按逻辑路径删除文件；默认异步软删除，立即隐藏并释放 tag |
| 单工作区语义检索 | `POST /workspaces/{workspace_name}/search` | 在一个指定工作区中检索，需要 `read` 或 `write` 权限 |
| 多工作区语义检索 | `POST /workspaces/multi_space/search` | 在请求体指定的多个工作区中检索，可访问工作区正常执行，不可访问工作区写入 `skipped_workspaces` |
| 文档片段 iframe 预览 | `POST /workspaces/{workspace_name}/preview-links` | 外部后端为检索命中的 chunk 换取短期 `preview_url`，外部前端只把该 URL 放入 iframe |
| 服务令牌管理 | `/service-tokens` 系列管理端点 | 由用户 JWT 鉴权，用于创建令牌、列出令牌、管理绑定、吊销令牌和查看密钥 |

### 2.5 管理端点详情

#### POST `/service-tokens` — 创建令牌

**鉴权**：JWT（`Authorization: Bearer <JWT>`）

**请求体 `ServiceTokenCreateRequest`：**

| 字段 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| `name` | string | 否 | max_length=128 | 令牌可读名称 |
| `workspaces` | array | **是** | min_length=1 | 绑定数组，每项含 `workspace_id`(int) 和 `permission`(`"read"` 或 `"write"`，正则校验) |

**响应 `201 Created`：**

```json
{
  "id": 1,
  "secret": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "name": "my-token",
  "created_at": "2026-05-06T10:00:00+00:00",
  "workspaces": [
    {"workspace_id": 3, "workspace_name": "MyWorkspace", "permission": "read"},
    {"workspace_id": 5, "workspace_name": "AnotherWS", "permission": "write"}
  ]
}
```

`secret` 会在创建时返回完整值；后续令牌创建者或管理员可通过 `GET /service-tokens/{id}/secret` 查看完整密钥。

#### GET `/service-tokens` — 列出令牌

**鉴权**：JWT

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `workspace_id` | int | 否 | 按工作区过滤 |

**响应**：`List[ServiceTokenListItem]`

```json
[
  {
    "id": 1,
    "name": "my-token",
    "secret_preview": "sk-****abcd",
    "revoked_at": null,
    "created_by_user_id": 5,
    "workspaces": [
      {"workspace_id": 3, "workspace_name": "MyWorkspace", "permission": "read"}
    ]
  }
]
```

管理员可看全部令牌；普通用户只能看到自己所属工作区绑定的令牌。

#### PATCH `/service-tokens/{token_id}/workspaces` — 管理绑定

**鉴权**：JWT（须为令牌创建者或管理员）

**请求体 `ServiceTokenPatchBindingsRequest`：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `add` | array | 否 | 新增绑定数组，每项含 `workspace_id`(int) 和 `permission`(`"read"` 或 `"write"`） |
| `update` | array | 否 | 更新绑定数组，每项含 `workspace_id`(int) 和 `permission` |
| `remove` | array | 否 | 删除绑定数组，每项含 `workspace_id`(int) |

三个操作可在一次请求中同时执行。`add` 已存在 → **409**；`update` 不存在 → **404**；已吊销令牌 → **400**。

**响应**：同 `ServiceTokenListItem`

#### DELETE `/service-tokens/{token_id}` — 吊销令牌

**鉴权**：JWT（须为令牌创建者或管理员）

**响应**：`{ "message": "Token revoked" }`

已吊销的令牌再次吊销返回 `"Token already revoked"`。

#### GET `/service-tokens/{token_id}/secret` — 查看完整密钥

**鉴权**：JWT（须为令牌创建者或管理员）

**响应**：`{ "secret": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" }`

### 2.6 服务端点详情

#### 2.6.0 GET `/workspaces` — 查询令牌可访问的工作空间

无需指定工作区名称，直接返回令牌所有绑定的工作空间信息。

**响应示例：**

```json
{
  "items": [
    {
      "id": 3,
      "name": "MyWorkspace",
      "slug": "my-workspace",
      "description": "法务资料库",
      "permission": "read"
    },
    {
      "id": 5,
      "name": "AnotherWS",
      "slug": "another-ws",
      "description": null,
      "permission": "write"
    }
  ],
  "workspaces": [
    {
      "id": 3,
      "name": "MyWorkspace",
      "slug": "my-workspace",
      "description": "法务资料库",
      "permission": "read"
    },
    {
      "id": 5,
      "name": "AnotherWS",
      "slug": "another-ws",
      "description": null,
      "permission": "write"
    }
  ]
}
```

`items` 为推荐读取字段；`workspaces` 为兼容字段，内容与 `items` 相同。

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 工作区 ID |
| `name` | string | 工作区名称（全局唯一） |
| `slug` | string | URL 友好的工作区标识 |
| `description` | string/null | 工作区描述 |
| `permission` | string | 令牌对该工作区的权限（`read` 或 `write`） |

---

#### 2.6.1 GET `/workspaces/{workspace_name}/tree` — 嵌套目录树

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `path_prefix` | string | 否 | `/` | 逻辑路径前缀。为 `/` 时从虚拟根建树；非 `/` 时该路径须已存在为**目录**，否则 **404**。 |

**响应示例：**

```json
{
  "path": "/",
  "kind": "dir",
  "name": "",
  "size": 0,
  "mime_type": null,
  "updated_at": null,
  "children": [
    {
      "path": "/docs",
      "kind": "dir",
      "name": "docs",
      "size": 0,
      "mime_type": null,
      "updated_at": "2026-04-20T12:00:00+00:00",
      "children": [
        {
          "path": "/docs/report.pdf",
          "kind": "file",
          "name": "report.pdf",
          "size": 1048576,
          "mime_type": "application/pdf",
          "updated_at": "2026-04-20T14:00:00+00:00",
          "children": null
        }
      ]
    }
  ]
}
```

**节点字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `path` | string | 规范化绝对逻辑路径 |
| `kind` | string | `"dir"` 或 `"file"` |
| `name` | string | 显示名称（虚拟根为空串） |
| `size` | int | 文件大小（字节）；目录为 `0` |
| `mime_type` | string/null | MIME 类型；目录为 `null` |
| `updated_at` | string/null | ISO 8601 时间戳 |
| `children` | array/null | 目录为数组，文件为 `null` |

**限制：** 子树节点数上限 **5000**，深度上限 **50**；超出 → **400**。

---

#### 2.6.2 GET `/workspaces/{workspace_name}/children` — 一级子项

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | **是** | 目录逻辑路径，须存在否则 **404** |

**响应示例：**

```json
[
  {
    "id": 10,
    "path": "/docs",
    "name": "docs",
    "kind": "dir",
    "size": 0,
    "mime_type": null,
    "updated_at": "2026-04-20T12:00:00+00:00"
  },
  {
    "id": 45,
    "path": "/readme.txt",
    "name": "readme.txt",
    "kind": "file",
    "size": 256,
    "mime_type": "text/plain",
    "updated_at": "2026-04-20T10:00:00+00:00"
  }
]
```

返回排序：目录在前、文件在后，按名称字母序。单目录子项上限 **1000**；超出 → **400**。

---

#### 2.6.3 GET `/workspaces/{workspace_name}/entries/by-prefix` — 按前缀扁平列表

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `url_prefix` | string | 否 | `/` | 逻辑路径前缀，返回该前缀下（含自身）的所有目录和文件 |
| `path_prefix` | string | 否 | `null` | 兼容别名，与 `url_prefix` 等价；两者同时提供时值须一致，否则 **400** |

**响应示例：**

```json
{
  "url_prefix": "/docs",
  "total": 3,
  "items": [
    {"id": 10, "path": "/docs", "name": "docs", "kind": "dir", "size": 0, "mime_type": null, "updated_at": "..."},
    {"id": 45, "path": "/docs/report.pdf", "name": "report.pdf", "kind": "file", "size": 1048576, "mime_type": "application/pdf", "updated_at": "..."},
    {"id": 47, "path": "/docs/readme.md", "name": "readme.md", "kind": "file", "size": 512, "mime_type": "text/markdown", "updated_at": "..."}
  ]
}
```

结果数上限 **5000**；超出 → **400**。

---

#### 2.6.4 GET `/workspaces/{workspace_name}/documents/by-path` — 文件元数据

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | **是** | **文件**完整逻辑路径；指向目录 → **400** |

**响应示例：**

```json
{
  "id": 789,
  "path": "/docs/report.pdf",
  "name": "report.pdf",
  "size": 1048576,
  "mime_type": "application/pdf",
  "owner_id": 5,
  "processing_status": "completed",
  "parser_type": "pdf",
  "created_at": "2026-04-20T10:00:00+00:00",
  "updated_at": "2026-04-20T12:00:00+00:00"
}
```

**`processing_status` 枚举值：**

| 值 | 说明 |
|------|------|
| `pending` | 刚创建，等待处理 |
| `parsing` | 正在解析文档内容 |
| `building_hierarchy` | 正在构建层级结构 |
| `embedding` | 正在向量化入库 |
| `completed` | 处理完成，可检索 |
| `failed` | 处理失败 |

文件不存在或已软删除 → **404**；路径为目录 → **400**。

---

#### 2.6.5 GET `/workspaces/{workspace_name}/documents/by-tag` — 按 tag 查询文件元数据

**Query 参数：**

| 参数 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| `tag` | string | **是** | min_length=1 | 工作区内精确匹配的文件 tag |

**响应示例：**

```json
{
  "id": 789,
  "path": "/docs/report.pdf",
  "name": "report.pdf",
  "size": 1048576,
  "mime_type": "application/pdf",
  "tag": "erp-contract-20260420",
  "processing_status": "completed",
  "updated_at": "2026-04-20T12:00:00+00:00"
}
```

`tag` 的唯一性限定在单个工作区内：不同工作区可以使用相同 tag。文件不存在、tag 未命中或目标文件已软删除 → **404**。

---

#### 2.6.6 POST `/workspaces/{workspace_name}/documents` — 上传新文件

**Content-Type：** `multipart/form-data`

**表单字段：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `path` | string | **是** | — | **父目录**逻辑路径；默认须已存在（除非传 `create_dirs=true`） |
| `file` | file | **是** | — | 上传文件 |
| `parser_type` | string | 否 | `auto` | 解析器类型 |
| `create_dirs` | bool | 否 | `false` | 为 `true` 时先自动创建 `path` 及其所有缺失父目录（`mkdir -p`）再上传；缺省 `false` 时父目录须已存在，否则 **400** |
| `tag` | string | 否 | `null` | 工作区内唯一的单文件业务标识；允许 `A-Za-z0-9._:-`，长度 1-128；空值按未设置处理 |

**`parser_type` 支持值：** `auto`、`pdf`、`docx`、`xlsx`、`pptx`、`txt`、`md`、`html`、`json`、`csv`、`epub`

该 service-token 上传接口不接收 `document_type`；新文件会按默认 `general` 文档类型进入后续分块流水线。如需在上传时指定 `manual` 或 `laws`，使用 JWT `POST /files/upload`。

**支持处理的 MIME 类型：**

| MIME 类型 | 对应文件格式 |
|-----------|-------------|
| `text/plain` | TXT |
| `text/markdown` | Markdown |
| `text/html` | HTML |
| `text/csv` | CSV |
| `application/pdf` | PDF |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | DOCX |
| `application/msword` | DOC |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | XLSX |
| `application/vnd.ms-excel` | XLS |
| `application/vnd.openxmlformats-officedocument.presentationml.presentation` | PPTX |
| `application/vnd.ms-powerpoint` | PPT |
| `application/json` | JSON |
| `application/epub+zip` | EPUB |

**文件大小限制：** **100 MB**；超出 → **413**。

**成功：** **201 Created**

**响应示例：**

```json
{
  "id": 456,
  "path": "/incoming/report.pdf",
  "name": "report.pdf",
  "owner_id": 5,
  "parent_id": 12,
  "is_directory": false,
  "size": 1048576,
  "mime_type": "application/pdf",
  "tag": "erp-contract-20260420",
  "created_at": "2026-04-20T10:00:00+00:00",
  "updated_at": "2026-04-20T10:00:00+00:00",
  "task_id": 789
}
```

`task_id` 为自动创建的 `process_document` 任务 ID（MIME 不在支持列表时为 `null`）。

**冲突：** 同路径已存在文件 → **409**（应改用 PUT 覆盖）；同工作区内 `tag` 已被其它文件占用 → **409**，`detail` 为 `Tag already in use`。

---

#### 2.6.7 PUT `/workspaces/{workspace_name}/documents/upsert-by-tag` — 按 tag 幂等写入文件

该接口面向外部系统按业务主键同步单文件：`tag` 是幂等键，`target_path` 是最终文件完整逻辑路径，multipart 中的文件名不决定 OpenRag 内部路径。

**Content-Type：** `multipart/form-data`

**表单字段：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `tag` | string | **是** | — | 工作区内唯一的单文件业务标识；允许 `A-Za-z0-9._:-`，长度 1-128 |
| `target_path` | string | **是** | — | **文件**完整逻辑路径，如 `/incoming/report.pdf`；必须包含文件名 |
| `file` | file | **是** | — | 新文件内容 |
| `parser_type` | string | 否 | `auto` | 解析器类型 |
| `create_dirs` | bool | 否 | `false` | 为 `true` 时自动创建 `target_path` 的缺失父目录 |

**行为：**

| 场景 | HTTP | `action` | 说明 |
|------|------|----------|------|
| 当前工作区不存在该 `tag` | **201** | `created` | 在 `target_path` 创建新文件 |
| 该 `tag` 已存在，且路径相同 | **200** | `updated` | 保持同一文件记录，替换内容并重新处理 |
| 该 `tag` 已存在，但路径不同 | **200** | `moved` | 保持同一文件记录，移动到 `target_path` 并替换内容 |

**响应示例：**

```json
{
  "id": 456,
  "path": "/archive/report.pdf",
  "name": "report.pdf",
  "owner_id": 5,
  "parent_id": 12,
  "is_directory": false,
  "size": 1048576,
  "mime_type": "application/pdf",
  "tag": "erp-contract-20260420",
  "created_at": "2026-04-20T10:00:00+00:00",
  "updated_at": "2026-04-20T10:05:00+00:00",
  "task_id": 790,
  "action": "moved"
}
```

`tag` 为空或格式非法 → **400**；缺少 `target_path` → **422**；`target_path` 已被其它活动文件占用 → **409**；目标路径或父路径正在删除中 → **409**。

---

#### 2.6.8 PUT `/workspaces/{workspace_name}/documents/by-path` — 覆盖已有文件

**Content-Type：** `multipart/form-data`

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | **是** | 要覆盖的**文件**完整逻辑路径 |

**表单字段：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `file` | file | **是** | — | 新文件内容 |
| `parser_type` | string | 否 | `auto` | 重新处理的解析器类型 |

**行为：** 覆盖 MinIO 存储，清除原有 chunks/向量，重新创建 `process_document` 任务；不会通过请求参数修改原文件的 `document_type`。

**成功：** **200 OK**

**响应结构：** 同上传接口（含 `task_id`）。

文件不存在 → **404**；路径为目录 → **400**；MIME 不支持处理 → **400**。

---

#### 2.6.9 DELETE `/workspaces/{workspace_name}/documents/by-path` — 按路径删除文件

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `path` | string | **是** | — | 要删除的**文件**完整逻辑路径 |
| `background` | bool | 否 | `true` | `true` 为异步软删除；`false` 为同步物理删除 |

**默认异步软删除（`background=true`）：**

```json
{
  "message": "File deletion queued",
  "task_id": 901,
  "async": true
}
```

响应状态码为 **202 Accepted**。文件会立即对列表、按路径查询、按 tag 查询、检索、预览和分享不可见；该文件原有 `tag` 会被清空并释放，后台任务继续清理对象存储、chunk 与向量数据。

**同步物理删除（`background=false`）：**

```json
{
  "message": "File deleted",
  "async": false
}
```

响应状态码为 **200 OK**。文件不存在或已软删除 → **404**；read-only 令牌调用 → **403**。

---

#### 2.6.10 POST `/workspaces/{workspace_name}/search` — 语义检索

**Content-Type：** `application/json`

**请求体 `ServiceSearchRequest`：**

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `query` | string | **是** | — | min_length=1 | 检索语句 |
| `path_prefix` | string | 否 | `null` | — | 非 `/` 时仅保留 `uri` 在该前缀下的命中 |
| `top_k` | int | 否 | `10` | gt=0, le=100 | 返回结果数 |
| `use_rerank` | bool | 否 | `true` | — | 是否使用 cross-encoder 重排 |
| `use_contextual_retrieval` | bool | 否 | `false` | — | 启用 L0→L1→L2 层级检索 |
| `contextual_l0_top_n` | int | 否 | `40` | ge=5, le=200 | L0 候选文件数 |
| `contextual_l1_top_n` | int | 否 | `30` | ge=5, le=200 | L1 检索深度 |
| `contextual_chunk_fetch_multiplier` | int | 否 | `4` | ge=1, le=20 | chunk 获取倍率 |
| `retrieval_strategy` | string | 否 | `"auto"` | — | 检索策略：auto/light/deep/precise/flat |
| `use_l1_llm_navigation` | bool | 否 | `false` | — | 启用 LLM 辅助 chunk 选择（需 OPENAI_API_KEY） |

> 工作区由 URL 路径决定，**body 中不要传 `workspace_id`**。`ServiceSearchRequest` 不包含 `vector_similarity_weight`（内部默认 1.0）。

**响应示例：**

```json
{
  "results": [
    {
      "text": "合同约定总金额为...",
      "score": 0.95,
      "file_id": 123,
      "chunk_id": "abc-456",
      "chunk_index": 0,
      "page": 1,
      "level": 0,
      "block_type": "text",
      "start_offset": 0,
      "end_offset": 500,
      "bbox_x0": null,
      "bbox_y0": null,
      "bbox_x1": null,
      "bbox_y1": null,
      "source_block_id": null,
      "source_char_start": null,
      "source_char_end": null,
      "filename": "report.pdf",
      "uri": "/docs/report.pdf",
      "object_key": "...",
      "object_url": "...",
      "local_chunk_path": "...",
      "text_preview": "...",
      "retrieval_strategy": "auto",
      "l1_llm_filtered": null
    }
  ],
  "total": 5,
  "query_time_ms": 123.45,
  "l1_llm_applied": null,
  "l1_llm_skip_reason": null
}
```

**`SearchResult` 字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | string | chunk 文本 |
| `score` | float | 相关性分数 |
| `file_id` | int | 源文件 ID |
| `chunk_id` | string/null | Milvus chunk 标识 |
| `chunk_index` | int/null | chunk 在文件中的位置 |
| `page` | int | 页码（默认 0） |
| `level` | int | 层级（默认 0） |
| `block_type` | string | 块类型（默认 `"text"`） |
| `start_offset` / `end_offset` | int | 源偏移范围 |
| `bbox_x0/y0/x1/y1` | float/null | PDF 块边界框 |
| `source_block_id` | string/null | 源块标识 |
| `source_char_start/end` | int/null | 源字符偏移 |
| `filename` | string/null | 源文件名 |
| `uri` | string/null | 源文件逻辑路径（`path_prefix` 过滤基于此字段） |
| `object_key` / `object_url` | string/null | MinIO 存储 |
| `local_chunk_path` | string/null | 本地 chunk 跷径 |
| `text_preview` | string/null | 短文本预览 |
| `retrieval_strategy` | string/null | 实际检索策略 |
| `l1_llm_filtered` | bool/null | L1 LLM 是否参与过滤 |

**`SearchResponse` 顶层字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `results` | array | 检索命中列表 |
| `total` | int | 命中数（`path_prefix` 过滤后） |
| `query_time_ms` | float | 查询耗时（毫秒） |
| `l1_llm_applied` | bool/null | L1 LLM 导航是否执行 |
| `l1_llm_skip_reason` | string/null | L1 LLM 被跳过的原因（如 `no_api_key`、`not_contextual`、`no_l1_hits`） |

---

#### 2.6.11 POST `/workspaces/{workspace_name}/preview-links` — 创建文档片段 iframe 预览链接

该接口用于把 service token 检索命中的某个文档 chunk 换成可嵌入 iframe 的短期预览链接。`X-OpenRag-Token` 只在接入方后端使用，浏览器 iframe 只使用短期 preview token；长期 service token 不得进入浏览器、页面源码、localStorage 或前端日志。

**典型调用流程：**

1. 外部后端使用 `X-OpenRag-Token` 调用 `POST /workspaces/{workspace_name}/search`，拿到命中的 `file_id`、`chunk_id` 和 `chunk_index`。
2. 用户在外部前端点击某个 chunk。
3. 外部前端把用户选择通知外部后端，不直接接触 `X-OpenRag-Token`。
4. 外部后端调用 OpenRag `POST /workspaces/{workspace_name}/preview-links` 创建 preview link。
5. OpenRag 返回完整 `preview_url`；外部前端只把该地址放入 iframe。

**鉴权：** Service Token（`X-OpenRag-Token`），需要该工作区 `read` 或 `write` 权限。

**Content-Type：** `application/json`

**请求体 `ServicePreviewLinkRequest`：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `file_id` | int | **是** | — | search 命中返回的源文件 ID，必须属于 URL 中的工作区 |
| `chunk_id` | string | **是** | — | search 命中返回的 chunk 标识，必须属于同一 `file_id` |
| `chunk_index` | int/null | 否 | `null` | 可选一致性校验；提供时必须与该 chunk 的实际序号一致 |
| `ttl_seconds` | int/null | 否 | `null` | 预览 token 有效期秒数；不传时默认 15 分钟或 900 秒，超过部署上限时按上限裁剪 |

**请求示例：**

```json
{
  "file_id": 123,
  "chunk_id": "abc-456",
  "chunk_index": 0,
  "ttl_seconds": 900
}
```

**响应 `200 OK`，`ServicePreviewLinkResponse`：**

```json
{
  "preview_url": "https://openrag.example.com/embed/document-preview#token=eyJ...",
  "expires_at": "2026-06-03T10:15:00+00:00",
  "ttl_seconds": 900
}
```

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `preview_url` | string | OpenRag 返回的完整 iframe 地址，包含 `/embed/document-preview#token=` |
| `expires_at` | string | preview token 过期时间，ISO 8601 格式 |
| `ttl_seconds` | int | 实际有效期秒数；可能因部署上限小于请求值 |

`preview_url` 是 OpenRag 返回的完整地址，接入方不要自己拼 preview token/preview_url，也不要从 `preview_url` 中拆 token 后转存为长期凭证。iframe 内部 API 会通过 `X-OpenRag-Preview-Token` 同源访问 `/embed/v1/**`，不需要为了 iframe 预览放开浏览器 CORS。

**iframe 示例：**

```html
<iframe
  src="https://openrag.example.com/embed/document-preview#token=eyJ..."
  sandbox="allow-scripts allow-same-origin"
  referrerpolicy="no-referrer"
  width="100%"
  height="720"
></iframe>
```

生产部署需要在 Web 侧配置 `PREVIEW_FRAME_ANCESTORS`，把接入方 iframe 父页面 origin 加入 CSP `frame-ancestors`。例如只允许 OpenRag 自身和接入方控制台嵌入：

```bash
PREVIEW_FRAME_ANCESTORS="'self' http://192.168.100.33:2026 http://192.168.100.32:2026 http://172.16.31.61:2026 http://172.16.31.156:2026"
```

**安全边界：**

- `X-OpenRag-Token` 只保存在接入方后端，不进入浏览器。
- preview token 是短期 bearer token，默认 15 分钟或 900 秒；被转发后，在有效期内可打开同一个文档 chunk。
- preview token 只能访问签发时绑定的 workspace/file/chunk，不能访问其它 workspace、file 或 chunk，也不能 search/upload/delete。
- 第一版不提供显式下载按钮，也不提供面向接入方的下载流程。iframe 内部预览 API 会获取渲染所需内容，因此 preview link 不是 DRM，也不承诺阻止截图、复制、网络抓包或通过开发者工具保存预览内容；如需更强内容保护，请在接入方业务层额外设计。

**常见错误：**

| 状态码 | 常见 `detail` / 消息 | 说明 |
|--------|---------------|------|
| **400** | `File is a directory` | `file_id` 指向目录 |
| **400** | `chunk_index does not match chunk` | 请求中的 `chunk_index` 与真实 chunk 不一致 |
| **401** | `Invalid or missing service token` | 缺少、错误或已吊销 `X-OpenRag-Token` |
| **403** | `Token not authorized for this workspace` | 令牌未绑定 URL 中的工作区 |
| **404** | `Workspace not found` | 工作区名不存在 |
| **404** | `File not found` | 文件不存在、已软删除，或文件不属于该工作区 |
| **404** | `Chunk not found` | chunk 不存在，或 chunk 不属于该 workspace/file |

---

#### 2.6.12 POST `/workspaces/multi_space/search` — 多工作区语义检索

**Content-Type：** `application/json`

`multi_space` 是保留的虚拟工作区名，用于表示“本次请求由请求体中的 `workspace_names` 指定多个真实工作区”。它不会按真实 `Workspace.name` 查询。

该接口采用**部分成功**策略：请求中的可访问工作区正常检索；不存在或当前 service token 无权限读取的工作区不会中止整次请求，而是记录到响应的 `skipped_workspaces` 中。缺少 token 或 token 无效仍返回 **401**。

**请求体 `ServiceMultiWorkspaceSearchRequest`：**

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `workspace_names` | array[string] | **是** | — | min_length=1, max_length=20 | 要检索的真实工作区名称列表，使用 `Workspace.name`；后端会 trim、去空并按首次出现顺序去重 |
| `query` | string | **是** | — | min_length=1 | 检索语句 |
| `path_prefix` | string/null | 否 | `null` | — | 非 `/` 时仅保留 `uri` 在该前缀下的命中；同一前缀应用于所有目标工作区 |
| `top_k` | int | 否 | `10` | gt=0, le=100 | 全局返回结果数；多工作区结果合并后按分数截断 |
| `use_rerank` | bool | 否 | `true` | — | 是否使用 cross-encoder 重排 |
| `use_contextual_retrieval` | bool | 否 | `false` | — | 启用 L0→L1→L2 层级检索 |
| `contextual_l0_top_n` | int | 否 | `40` | ge=5, le=200 | L0 候选文件数 |
| `contextual_l1_top_n` | int | 否 | `30` | ge=5, le=200 | L1 检索深度 |
| `contextual_chunk_fetch_multiplier` | int | 否 | `4` | ge=1, le=20 | chunk 获取倍率 |
| `retrieval_strategy` | string | 否 | `"auto"` | — | 检索策略：auto/light/deep/precise/flat |
| `use_l1_llm_navigation` | bool | 否 | `false` | — | 启用 LLM 辅助 chunk 选择（需 OPENAI_API_KEY） |

**行为规则：**

- `workspace_names` 缺失、为空或去重后为空 → **400**，`detail` 为 `workspace_names is required for multi_space search`。
- 请求中的工作区不存在时，该工作区进入 `skipped_workspaces`，`reason` 为 `not_found`。
- 当前 service token 对某工作区无 read/write 权限时，该工作区进入 `skipped_workspaces`，`reason` 为 `permission_denied`。
- 只传 1 个可访问工作区时，检索行为与单工作区 `/workspaces/{workspace_name}/search` 一致，但响应仍包含多工作区接口的顶层字段。
- 传多个可访问工作区时，后端分别在这些工作区内检索，结果补充 `workspace_id` / `workspace_name` 后合并排序。
- 如果所有请求工作区都不可检索，仍返回 **200**，`results=[]`、`total=0`、`workspace_count=0`，并通过 `skipped_workspaces` 说明原因。
- 为避免误用，真实工作区不应命名为 `multi_space`。

**请求示例：**

```json
{
  "workspace_names": ["MyWorkspace", "AnotherWS"],
  "query": "合同金额",
  "top_k": 5,
  "path_prefix": "/法务",
  "use_rerank": true,
  "use_contextual_retrieval": false,
  "contextual_l0_top_n": 40,
  "contextual_l1_top_n": 30,
  "contextual_chunk_fetch_multiplier": 4,
  "retrieval_strategy": "auto",
  "use_l1_llm_navigation": false
}
```

**响应示例：**

```json
{
  "results": [
    {
      "workspace_id": 3,
      "workspace_name": "MyWorkspace",
      "text": "合同约定总金额为...",
      "score": 0.95,
      "file_id": 123,
      "chunk_id": "abc-456",
      "chunk_index": 0,
      "page": 1,
      "level": 0,
      "block_type": "text",
      "start_offset": 0,
      "end_offset": 500,
      "bbox_x0": null,
      "bbox_y0": null,
      "bbox_x1": null,
      "bbox_y1": null,
      "source_block_id": null,
      "source_char_start": null,
      "source_char_end": null,
      "filename": "contract.pdf",
      "uri": "/法务/contract.pdf",
      "object_key": "...",
      "object_url": "...",
      "local_chunk_path": "...",
      "text_preview": "...",
      "retrieval_strategy": "auto",
      "l1_llm_filtered": null
    }
  ],
  "total": 1,
  "query_time_ms": 245.67,
  "workspace_count": 1,
  "l1_llm_applied": null,
  "l1_llm_skip_reason": null,
  "skipped_workspaces": [
    {
      "workspace_name": "AnotherWS",
      "reason": "permission_denied",
      "message": "Token does not have read permission for this workspace"
    }
  ]
}
```

**`MultiWorkspaceSearchResponse` 顶层字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `results` | array | 合并、排序、截断后的检索命中列表 |
| `total` | int | 最终返回的命中条数，即 `results.length` |
| `query_time_ms` | float | 所有实际参与检索工作区的查询耗时汇总（毫秒） |
| `workspace_count` | int | 去重后实际参与检索的工作区数量；被跳过的工作区不计入 |
| `l1_llm_applied` | bool/null | 任一工作区实际应用 L1 LLM 导航时为 `true`；全部为空时为 `null` |
| `l1_llm_skip_reason` | string/null | 未应用 L1 LLM 的原因；多种原因混合时为 `"mixed"` |
| `skipped_workspaces` | array | 被跳过的工作区列表，可能为空数组 |

**`skipped_workspaces[]` 字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `workspace_name` | string | 请求中被跳过的工作区名称 |
| `reason` | string | 跳过原因：`not_found` 或 `permission_denied` |
| `message` | string | 面向调用方的原因说明 |

**多工作区命中字段补充：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `workspace_id` | int | 命中所属工作区 ID |
| `workspace_name` | string | 命中所属工作区名称 |

其余命中字段与单工作区 `SearchResult` 一致。

---

#### 2.6.13 GET `/workspaces/{workspace_name}/documents/search-by-name` — 按文件名模糊搜索

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `filename` | string | 否 | `""` | — | 文件名子串，大小写不敏感；`%` 和 `_` 自动转义；为空时返回所有文件 |
| `path_prefix` | string | 否 | `/` | — | 限定搜索范围到该路径前缀下（含自身） |
| `skip` | int | 否 | `0` | ge=0 | 分页偏移 |
| `limit` | int | 否 | `50` | ge=1, le=200 | 分页大小 |

仅返回文件（排除目录），按文件名字母序排序。

**响应示例：**

```json
{
  "items": [
    {
      "id": 45,
      "path": "/docs/report.pdf",
      "name": "report.pdf",
      "size": 1048576,
      "mime_type": "application/pdf",
      "tag": "erp-contract-20260420",
      "processing_status": "completed",
      "updated_at": "2026-04-20T14:00:00+00:00"
    }
  ],
  "total": 12,
  "skip": 0,
  "limit": 50
}
```

### 2.7 调用示例

#### cURL

**查询令牌可访问的工作空间：**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxxxxxxx" \
  "https://api.example.com/service/v1/workspaces"
```

**列根目录树：**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxxxxxxx" \
  "https://api.example.com/service/v1/workspaces/MyWorkspace/tree?path_prefix=%2F"
```

**语义检索：**

```bash
curl -sS -X POST "https://api.example.com/service/v1/workspaces/MyWorkspace/search" \
  -H "X-OpenRag-Token: sk-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"query":"合同金额","top_k":5,"path_prefix":"/法务"}'
```

**多工作区语义检索：**

```bash
curl -sS -X POST "https://api.example.com/service/v1/workspaces/multi_space/search" \
  -H "X-OpenRag-Token: sk-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"workspace_names":["MyWorkspace","AnotherWS"],"query":"合同金额","top_k":5,"path_prefix":"/法务"}'
```

**创建文档片段预览链接：**

```bash
curl -sS -X POST "https://api.example.com/service/v1/workspaces/MyWorkspace/preview-links" \
  -H "X-OpenRag-Token: sk-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"file_id":123,"chunk_id":"abc-456","chunk_index":0,"ttl_seconds":900}'
```

外部前端使用响应中的完整 `preview_url`：

```html
<iframe
  src="https://openrag.example.com/embed/document-preview#token=eyJ..."
  sandbox="allow-scripts allow-same-origin"
  referrerpolicy="no-referrer"
></iframe>
```

**上传文件：**

```bash
curl -sS -X POST "https://api.example.com/service/v1/workspaces/MyWorkspace/documents" \
  -H "X-OpenRag-Token: sk-xxxxxxxx" \
  -F "path=/incoming" \
  -F "tag=erp-contract-20260420" \
  -F "file=@./local.pdf"
```

**按 tag 幂等写入文件：**

```bash
curl -sS -X PUT "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/upsert-by-tag" \
  -H "X-OpenRag-Token: sk-xxxxxxxx" \
  -F "tag=erp-contract-20260420" \
  -F "target_path=/archive/report.pdf" \
  -F "create_dirs=true" \
  -F "file=@./local.pdf"
```

**覆盖已有文件：**

```bash
curl -sS -X PUT "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/by-path?path=%2Fincoming%2Freport.pdf" \
  -H "X-OpenRag-Token: sk-xxxxxxxx" \
  -F "file=@./updated.pdf"
```

**按路径取文件元数据：**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxxxxxxx" \
  "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/by-path?path=%2Fdocs%2Freport.pdf"
```

**按 tag 取文件元数据：**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxxxxxxx" \
  "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/by-tag?tag=erp-contract-20260420"
```

**按文件名模糊搜索：**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxxxxxxx" \
  "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/search-by-name?filename=report&path_prefix=%2Fdocs"
```

**按路径删除文件（默认异步软删除）：**

```bash
curl -sS -X DELETE "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/by-path?path=%2Fdocs%2Freport.pdf" \
  -H "X-OpenRag-Token: sk-xxxxxxxx"
```

#### Python（requests）

```python
import requests

BASE = "https://api.example.com/service/v1"
WS = "MyWorkspace"
TOKEN = "sk-...."
HEADERS = {"X-OpenRag-Token": TOKEN}
WS_URL = f"{BASE}/workspaces/{requests.utils.quote(WS)}"

# 查询令牌可访问的工作空间
r = requests.get(f"{BASE}/workspaces", headers=HEADERS, timeout=30)
r.raise_for_status()
print(r.json())

# 列目录树
r = requests.get(f"{WS_URL}/tree", params={"path_prefix": "/"}, headers=HEADERS, timeout=60)
r.raise_for_status()
print(r.json())

# 语义检索
r = requests.post(
    f"{WS_URL}/search",
    json={"query": "合同金额", "top_k": 5, "path_prefix": "/法务"},
    headers=HEADERS,
    timeout=60,
)
r.raise_for_status()
print(r.json())

# 多工作区语义检索
r = requests.post(
    f"{BASE}/workspaces/multi_space/search",
    json={
        "workspace_names": ["MyWorkspace", "AnotherWS"],
        "query": "合同金额",
        "top_k": 5,
        "path_prefix": "/法务",
    },
    headers=HEADERS,
    timeout=60,
)
r.raise_for_status()
print(r.json())

# 创建文档片段 iframe 预览链接。外部后端执行本请求，外部前端只使用返回的 preview_url。
r = requests.post(
    f"{WS_URL}/preview-links",
    json={
        "file_id": 123,
        "chunk_id": "abc-456",
        "chunk_index": 0,
        "ttl_seconds": 900,
    },
    headers=HEADERS,
    timeout=30,
)
r.raise_for_status()
preview_url = r.json()["preview_url"]
iframe_html = (
    f'<iframe src="{preview_url}" '
    'sandbox="allow-scripts allow-same-origin" '
    'referrerpolicy="no-referrer"></iframe>'
)
print(iframe_html)

# 上传文件
r = requests.post(
    f"{WS_URL}/documents",
    data={"path": "/incoming", "tag": "erp-contract-20260420"},
    files={"file": open("local.pdf", "rb")},
    headers=HEADERS,
    timeout=120,
)
r.raise_for_status()
print(r.json())

# 按 tag 幂等写入文件
r = requests.put(
    f"{WS_URL}/documents/upsert-by-tag",
    data={
        "tag": "erp-contract-20260420",
        "target_path": "/archive/report.pdf",
        "create_dirs": "true",
    },
    files={"file": open("local.pdf", "rb")},
    headers=HEADERS,
    timeout=120,
)
r.raise_for_status()
print(r.json())

# 覆盖文件
r = requests.put(
    f"{WS_URL}/documents/by-path",
    params={"path": "/incoming/report.pdf"},
    files={"file": open("updated.pdf", "rb")},
    headers=HEADERS,
    timeout=120,
)
r.raise_for_status()
print(r.json())

# 文件元数据
r = requests.get(
    f"{WS_URL}/documents/by-path",
    params={"path": "/docs/report.pdf"},
    headers=HEADERS,
    timeout=30,
)
r.raise_for_status()
print(r.json())

# 按 tag 查询文件元数据
r = requests.get(
    f"{WS_URL}/documents/by-tag",
    params={"tag": "erp-contract-20260420"},
    headers=HEADERS,
    timeout=30,
)
r.raise_for_status()
print(r.json())

# 按文件名模糊搜索
r = requests.get(
    f"{WS_URL}/documents/search-by-name",
    params={"filename": "report", "path_prefix": "/docs", "limit": 10},
    headers=HEADERS,
    timeout=30,
)
r.raise_for_status()
print(r.json())

# 按路径删除文件（默认异步软删除）
r = requests.delete(
    f"{WS_URL}/documents/by-path",
    params={"path": "/docs/report.pdf"},
    headers=HEADERS,
    timeout=30,
)
r.raise_for_status()
print(r.json())
```

### 2.8 HTTP 状态码速查

| 状态码 | 常见 `detail` / 消息 | 说明 |
|--------|---------------|------|
| **202** | `File deletion queued` | 文件已进入异步软删除队列 |
| **400** | `Invalid path: path traversal detected` | 路径含 `..` |
| **400** | `Too many nodes under path (limit 5000)` | 子树节点超出上限 |
| **400** | `Tree too deep (limit 50)` | 树深度超出上限 |
| **400** | `Too many entries in directory (limit 1000)` | 单目录子项超出上限 |
| **400** | `Path is a directory, not a file` | documents/by-path 指向目录 |
| **400** | `Parent directory does not exist` | 上传时父目录不存在 |
| **400** | `Invalid parser_type. Supported types: ...` | parser_type 值不在支持列表 |
| **400** | `Invalid tag. Allowed: ^[A-Za-z0-9._:-]{1,128}$` | tag 格式非法 |
| **400** | `upsert-by-tag requires a non-empty tag` | 按 tag 幂等写入时 tag 为空 |
| **400** | `target_path must be a full file path (file name required)` | upsert 的目标路径未包含文件名 |
| **400** | `url_prefix and path_prefix must be the same when both provided` | 两个前缀参数值不一致 |
| **400** | `Cannot replace directory content` | 覆盖路径指向目录 |
| **400** | `Cannot modify bindings on a revoked token` | 修改已吊销令牌的绑定 |
| **400** | `Duplicate workspace binding` | 创建令牌时请求体中重复绑定同一工作区 |
| **400** | `Invalid document_type. Supported types: ...` | JWT 上传或重处理时文档内容类型不在支持列表 |
| **400** | `File type ... is not supported for processing` | 覆盖时文件 MIME 不在支持列表 |
| **400** | `workspace_names is required for multi_space search` | 多工作区检索缺少目标工作区数组 |
| **400** | `File is a directory` | 创建 preview link 时 `file_id` 指向目录 |
| **400** | `chunk_index does not match chunk` | 创建 preview link 时 chunk 序号校验失败 |
| **401** | `Invalid or missing service token` | 缺头、密钥错、已吊销 |
| **401** | `Preview token required` | iframe 内部预览 API 缺少 `X-OpenRag-Preview-Token` |
| **401** | `Invalid preview token` | preview token 无效、过期或签名错误 |
| **403** | `Token has no workspace bindings` | 令牌存在但没有任何工作区绑定 |
| **403** | `Token not authorized for this workspace` | 单工作区接口中，令牌未绑定该工作区 |
| **403** | `Token permission insufficient` | 令牌对绑定的工作区权限不足 |
| **404** | `Workspace not found` | 单工作区接口中，工作区名不存在 |
| **404** | `Directory not found` | 目录路径不存在 |
| **404** | `File not found` | 文件路径不存在 |
| **404** | `No document with this tag` | 按 tag 查询未命中，或文件已软删除 |
| **404** | `Chunk not found` | 创建 preview link 时 chunk 不属于该 workspace/file |
| **409** | `File already exists at <uri>` | 上传时目标路径已有文件 |
| **409** | `Tag already in use` | 同工作区内 tag 已被活动文件占用 |
| **409** | `Path is pending deletion; retry after cleanup completes` | 目标路径或祖先路径仍在异步删除清理中 |
| **409** | `Target path already occupied by another document: <uri>` | upsert 目标路径被其它活动文件占用 |
| **409** | `Token already has a binding for this workspace` | 添加已存在的绑定 |
| **413** | `File size exceeds maximum allowed size of 100.0MB` | 上传文件超过 100 MB |
| **422** | — | multipart/form-data 缺少必填字段，如 upsert 缺少 `target_path` |
| **500** | — | 内部错误或检索执行失败 |

多工作区检索是例外：请求中某个工作区不存在或无读取权限时，整体仍返回 **200**，该工作区会出现在 `skipped_workspaces`，`reason` 分别为 `not_found` 或 `permission_denied`。

### 2.9 排障指南

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| 401 `Invalid or missing service token` | 头缺失、密钥错误、令牌已吊销 | 检查 `X-OpenRag-Token` 头是否设置、密钥是否完整、是否已被吊销 |
| 403 `Token not authorized for this workspace` | 令牌未绑定该工作区 | 在「管理绑定」中为令牌添加该工作区的绑定 |
| 403 `Token permission insufficient` | 令牌对绑定的工作区权限不足（read 调用 write 接口） | 将绑定权限升级为 write |
| 多工作区检索返回 200，但某些工作区没有结果 | 工作区不存在，或当前 service token 没有该工作区 read/write 权限 | 查看 `skipped_workspaces` 中的 `reason` 和 `message`，补齐绑定或修正工作区名称 |
| 404 `Directory not found` | `path_prefix` 或 `path` 在库中不存在 | 确认目录路径已通过上传或 Web 端创建 |
| 409 `File already exists` | 上传路径已有同名文件 | 改用 PUT 覆盖 |
| 409 `Tag already in use` | 同工作区已有活动文件使用该业务 tag | 如果是同一业务文件，改用 `PUT /documents/upsert-by-tag`；否则更换 tag |
| 409 `Path is pending deletion` | 目标路径或其父路径刚被异步删除，物理清理未完成 | 等后台删除任务完成后重试，或改用其它路径 |
| 400 `Parent directory does not exist` | 上传时父目录未创建 | 上传时带 `create_dirs=true` 自动建目录；或先通过 Web 端 / JWT `POST /files/directories` 创建目录 |
| 404 `No document with this tag` | tag 不存在，或文件已被软删除并释放 tag | 确认工作区名和 tag；如刚删除过文件，可重新上传或 upsert |
| 检索返回 0 结果 | 文件 `processing_status` 非 `completed` | 等待文件处理完成再检索 |
| 文件名搜索返回 0 结果 | 文件名不匹配或 `path_prefix` 限定范围内无文件 | 尝试缩短关键词或扩大 path_prefix 范围 |
| 创建 preview link 返回 404 `File not found` 或 `Chunk not found` | 前端传回的 `file_id`、`chunk_id` 与当前工作区或文件不匹配 | 使用 search 响应原样传递这些字段，不要按文件名或路径自行定位 chunk |
| 创建 preview link 返回 400 `chunk_index does not match chunk` | `chunk_index` 不是该 chunk 的真实序号 | 使用 search 返回的 `chunk_index`，或不传该可选字段 |
| iframe 空白或被浏览器拒绝嵌入 | `PREVIEW_FRAME_ANCESTORS` 未包含接入方父页面 origin | 在 Web 部署配置中加入接入方 origin，例如 `http://192.168.100.33:2026` |
| iframe 内预览 401 | `preview_url` 过期，或接入方自行拼接/截断了 token | 重新向外部后端请求 preview link；接入方不要自己拼 preview token/preview_url |

### 2.10 安全建议

- 生产环境对 `/service/v1` 应做 **HTTPS 终止**（Ingress / 网关）。
- 网关侧建议配置 **IP 白名单**、**速率限制**。
- 密钥应定期轮换：吊销旧令牌、创建新令牌、更新外部系统配置。
- 禁止在日志中输出完整 `X-OpenRag-Token` 或 `secret` 值。
- `write` 令牌具有完整读写权限，仅在必要时发放；日常检索使用 `read` 令牌即可。
- 创建 preview link 应在接入方后端完成；浏览器 iframe 只接收 OpenRag 返回的 `preview_url`，不要暴露长期 `X-OpenRag-Token`。
- preview token 是短期 bearer token，默认 15 分钟或 900 秒；外部系统不要持久化为长期链接，过期后重新创建。
- `PREVIEW_FRAME_ANCESTORS` 只控制哪些父页面可嵌入 OpenRag 预览页；iframe 内 API 是同源访问，不需要为了预览扩大浏览器 CORS。
- 第一版 iframe 预览不提供显式下载按钮，也不提供面向接入方的下载流程；但 iframe 内部预览 API 会获取渲染所需内容，preview link 不是 DRM，不能阻止截图、复制、网络抓包或通过开发者工具保存已渲染内容。

### 2.11 与 OpenAPI 对齐

运行中服务可访问 **`/docs`**（Swagger UI）或 **`/redoc`**，在 **service** 和 **service-tokens** 标签下查看完整模型定义并试调（试调时在 security 输入框填入 `X-OpenRag-Token` 值）。

### 2.12 相关脚本与设计

- 数据库迁移脚本：`openrag/scripts/sql/2026-05-06-service-token-multi-workspace.sql`
- 通用目录批量导入脚本（JWT `POST /files/upload`）：`openrag/scripts/bulk_import_folder.py`，可保持本地相对目录结构上传，并支持 `--dry-run` 预览。
- 设计背景：`docs/superpowers/specs/2026-05-06-service-token-multi-workspace-design.md`
