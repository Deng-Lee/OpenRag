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
2. 记录返回的完整密钥（`sk-` 前缀）；仅创建时可见明文，后续只能吊销或改权限。
3. 外部系统保存密钥于 **Secret 管理**（环境变量、Vault、K8s Secret），禁止写入前端或版本库。
4. 调用时统一设置请求头：`X-OpenRag-Token: <完整密钥>`。
5. URL 中的工作区标识为工作区的 **`name`（全局唯一）**，含中文或空格时需 **URL 编码**；令牌须已绑定该工作区，否则 **403**。

### 1.3 用户 JWT API（管理 / 人机）

面向交互式与「带用户身份」的集成，可直接使用 FastAPI 暴露的路由（前缀均相对于 API 根，无 `/api`；若经网关挂载 `/api`，请自行剥离前缀）。

**获取令牌**：`POST /users/login`、注册 `POST /users/register` 等——见 `/docs` 中 **users** 标签。

**常用前缀**（摘录，以 OpenAPI 为准）：

| 前缀 | 说明 |
|------|------|
| `/workspaces` | 工作区 CRUD、成员 |
| `/files` | 文件树、上传、权限相关子路径 |
| `/search` | 语义检索（JWT） |
| `/tasks`、`/broker` | 任务与调度运维 |
| `/roles` | 角色与授权（管理用） |
| `/service-tokens` | 服务令牌管理（CRUD、绑定、吊销）——见第二部分 |

完整契约：**部署后打开** `https://<api-host>/docs` 或 `https://<domain>/api/docs`（若使用 Nginx `/api` 代理）。

### 1.4 网络、TLS 与限流

- 生产环境应对公网 **HTTPS** 终止（Ingress / 网关），后端可只接收集群内 HTTP。
- 上传体积受 `MAX_UPLOAD_SIZE` 等配置约束。
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
3. **调用接口** — 所有请求携带 `X-OpenRag-Token` 头即可访问 `/service/v1` 下的 9 个机读接口。

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
| **绑定工作区** | 令牌可授权绑定多个工作区，每个工作区拥有独立权限等级。可通过「管理绑定」接口增删改绑定。调用 `/workspaces/{workspace_name}` 接口时，令牌须已绑定该工作区，否则 **403**。 |
| **权限等级** | 每个绑定独立设置 `read`（只读）或 `write`（读写）。`write` 包含 `read`。可在「管理绑定」接口修改。 |
| **吊销** | 吊销后立即对所有 `/service/v1` 接口失效。 |

#### 2.2.3 权限矩阵

| 操作 | 所需令牌权限 |
|------|----------------|
| 目录树、列子项、按前缀查询、文件元数据、语义检索、按文件名搜索 | **read** 或 **write** |
| 上传新文件、覆盖已有文件 | **write** |

只读令牌调用写接口 → **403**，`detail`：`Write permission required`。

### 2.3 前置概念

| 概念 | 说明 |
|------|------|
| **工作区标识（URL）** | 路径中使用工作区的 **`name`**（全局唯一展示名），不是数字 `id`。含中文或空格时需 **URL 编码**。 |
| **逻辑路径** | 文件在工作区内的路径，**必须以 `/` 开头**（如 `/`、`/docs/report.pdf`）。禁止 `..` 穿越，检测到 → **400**。 |
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
| `POST` | `/workspaces/{workspace_name}/documents` | 上传新文件（multipart） | write |
| `PUT` | `/workspaces/{workspace_name}/documents/by-path` | 覆盖已有文件 | write |
| `POST` | `/workspaces/{workspace_name}/search` | 语义检索（JSON body） | read |
| `POST` | `/workspaces/multi_space/search` | 多工作区语义检索（JSON body） | read |
| `GET` | `/workspaces/{workspace_name}/documents/search-by-name` | 按文件名子串模糊搜索 | read |

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

`secret` 仅在创建时返回完整值，后续不可再获取明文（可通过 `GET /service-tokens/{id}/secret` 查看）。

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
  "workspaces": [
    {
      "name": "MyWorkspace",
      "slug": "my-workspace",
      "permission": "read"
    },
    {
      "name": "AnotherWS",
      "slug": "another-ws",
      "permission": "write"
    }
  ]
}
```

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 工作区名称（全局唯一） |
| `slug` | string | URL 友好的工作区标识 |
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

文件不存在 → **404**；路径为目录 → **400**。

---

#### 2.6.5 POST `/workspaces/{workspace_name}/documents` — 上传新文件

**Content-Type：** `multipart/form-data`

**表单字段：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `path` | string | **是** | — | **父目录**逻辑路径；须已存在，不会自动创建缺失目录 |
| `file` | file | **是** | — | 上传文件 |
| `parser_type` | string | 否 | `auto` | 解析器类型 |

**`parser_type` 支持值：** `auto`、`pdf`、`docx`、`xlsx`、`pptx`、`txt`、`md`、`html`、`json`、`csv`、`epub`

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
  "created_at": "2026-04-20T10:00:00+00:00",
  "updated_at": "2026-04-20T10:00:00+00:00",
  "task_id": 789
}
```

`task_id` 为自动创建的 `process_document` 任务 ID（MIME 不在支持列表时为 `null`）。

**冲突：** 同路径已存在文件 → **409**（应改用 PUT 覆盖）。

---

#### 2.6.6 PUT `/workspaces/{workspace_name}/documents/by-path` — 覆盖已有文件

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

**行为：** 覆盖 MinIO 存储，清除原有 chunks/向量，重新创建 `process_document` 任务。

**成功：** **200 OK**

**响应结构：** 同上传接口（含 `task_id`）。

文件不存在 → **404**；路径为目录 → **400**；MIME 不支持处理 → **400**。

---

#### 2.6.7 POST `/workspaces/{workspace_name}/search` — 语义检索

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

#### 2.6.8 POST `/workspaces/multi_space/search` — 多工作区语义检索

**Content-Type：** `application/json`

`multi_space` 是保留的虚拟工作区名，用于表示“本次请求由请求体中的 `workspace_names` 指定多个真实工作区”。它不会按真实 `Workspace.name` 查询。

**请求体 `ServiceMultiWorkspaceSearchRequest`：**

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `workspace_names` | array[string] | **是** | — | min_length=1, max_length=20 | 要检索的真实工作区名称列表；每个工作区均需当前 service token 具备 read 权限 |
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

- `workspace_names` 为空或缺失 → **400**。
- `workspace_names` 中任一工作区不存在 → **404**。
- 当前 service token 对任一工作区无 read/write 权限 → **403**。
- 只传 1 个工作区时，行为等价于单工作区 `/workspaces/{workspace_name}/search`。
- 传多个工作区时，后端分别在这些工作区内检索，结果补充 `workspace_id` / `workspace_name` 后合并排序。
- 为避免误用，真实工作区不应命名为 `multi_space`。

**请求示例：**

```json
{
  "workspace_names": ["MyWorkspace", "AnotherWS"],
  "query": "合同金额",
  "top_k": 5,
  "path_prefix": "/法务",
  "use_rerank": true,
  "use_contextual_retrieval": false
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
    },
    {
      "workspace_id": 5,
      "workspace_name": "AnotherWS",
      "text": "补充协议中约定...",
      "score": 0.88,
      "file_id": 456,
      "chunk_id": "def-789",
      "chunk_index": 2,
      "page": 3,
      "level": 0,
      "block_type": "text",
      "start_offset": 120,
      "end_offset": 420,
      "bbox_x0": null,
      "bbox_y0": null,
      "bbox_x1": null,
      "bbox_y1": null,
      "source_block_id": null,
      "source_char_start": null,
      "source_char_end": null,
      "filename": "agreement.pdf",
      "uri": "/法务/agreement.pdf",
      "object_key": "...",
      "object_url": "...",
      "local_chunk_path": "...",
      "text_preview": "...",
      "retrieval_strategy": "auto",
      "l1_llm_filtered": null
    }
  ],
  "total": 2,
  "query_time_ms": 245.67,
  "workspace_count": 2,
  "l1_llm_applied": null,
  "l1_llm_skip_reason": null
}
```

**多工作区命中字段补充：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `workspace_id` | int | 命中所属工作区 ID |
| `workspace_name` | string | 命中所属工作区名称 |

其余命中字段与单工作区 `SearchResult` 一致。`total` 为多工作区合并、过滤、截断后的返回条数；`workspace_count` 为本次请求参与检索的工作区数量。

---

#### 2.6.9 GET `/workspaces/{workspace_name}/documents/search-by-name` — 按文件名模糊搜索

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

**上传文件：**

```bash
curl -sS -X POST "https://api.example.com/service/v1/workspaces/MyWorkspace/documents" \
  -H "X-OpenRag-Token: sk-xxxxxxxx" \
  -F "path=/incoming" \
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

**按文件名模糊搜索：**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxxxxxxx" \
  "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/search-by-name?filename=report&path_prefix=%2Fdocs"
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

# 上传文件
r = requests.post(
    f"{WS_URL}/documents",
    data={"path": "/incoming"},
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

# 按文件名模糊搜索
r = requests.get(
    f"{WS_URL}/documents/search-by-name",
    params={"filename": "report", "path_prefix": "/docs", "limit": 10},
    headers=HEADERS,
    timeout=30,
)
r.raise_for_status()
print(r.json())
```

### 2.8 HTTP 状态码速查

| 状态码 | 常见 `detail` | 说明 |
|--------|---------------|------|
| **400** | `Invalid path: path traversal detected` | 路径含 `..` |
| **400** | `Too many nodes under path (limit 5000)` | 子树节点超出上限 |
| **400** | `Tree too deep (limit 50)` | 树深度超出上限 |
| **400** | `Too many entries in directory (limit 1000)` | 单目录子项超出上限 |
| **400** | `Path is a directory, not a file` | documents/by-path 指向目录 |
| **400** | `Parent directory does not exist` | 上传时父目录不存在 |
| **400** | `Invalid parser_type. Supported types: ...` | parser_type 值不在支持列表 |
| **400** | `url_prefix and path_prefix must be the same when both provided` | 两个前缀参数值不一致 |
| **400** | `Cannot replace directory content` | 覆盖路径指向目录 |
| **400** | `Cannot modify bindings on a revoked token` | 修改已吊销令牌的绑定 |
| **400** | `File type ... is not supported for processing` | 覆盖时文件 MIME 不在支持列表 |
| **400** | `workspace_names is required for multi_space search` | 多工作区检索缺少目标工作区数组 |
| **401** | `Invalid or missing service token` | 缺头、密钥错、已吊销 |
| **403** | `Token not authorized for this workspace` | 令牌未绑定该工作区 |
| **403** | `Token permission insufficient` | 令牌对绑定的工作区权限不足 |
| **404** | `Workspace not found` | 工作区名不存在 |
| **404** | `Directory not found` | 目录路径不存在 |
| **404** | `File not found` | 文件路径不存在 |
| **409** | `File already exists at <uri>` | 上传时目标路径已有文件 |
| **409** | `Binding already exists` | 添加已存在的绑定 |
| **413** | `File size exceeds maximum allowed size of 100.0MB` | 上传文件超过 100 MB |
| **500** | — | 内部错误或检索执行失败 |

### 2.9 排障指南

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| 401 `Invalid or missing service token` | 头缺失、密钥错误、令牌已吊销 | 检查 `X-OpenRag-Token` 头是否设置、密钥是否完整、是否已被吊销 |
| 403 `Token not authorized for this workspace` | 令牌未绑定该工作区 | 在「管理绑定」中为令牌添加该工作区的绑定 |
| 403 `Token permission insufficient` | 令牌对绑定的工作区权限不足（read 调用 write 接口） | 将绑定权限升级为 write |
| 404 `Directory not found` | `path_prefix` 或 `path` 在库中不存在 | 确认目录路径已通过上传或 Web 端创建 |
| 409 `File already exists` | 上传路径已有同名文件 | 改用 PUT 覆盖 |
| 400 `Parent directory does not exist` | 上传时父目录未创建 | 先通过 Web 端或 POST `/files/directories`（JWT）创建目录 |
| 检索返回 0 结果 | 文件 `processing_status` 非 `completed` | 等待文件处理完成再检索 |
| 文件名搜索返回 0 结果 | 文件名不匹配或 `path_prefix` 限定范围内无文件 | 尝试缩短关键词或扩大 path_prefix 范围 |

### 2.10 安全建议

- 生产环境对 `/service/v1` 应做 **HTTPS 终止**（Ingress / 网关）。
- 网关侧建议配置 **IP 白名单**、**速率限制**。
- 密钥应定期轮换：吊销旧令牌、创建新令牌、更新外部系统配置。
- 禁止在日志中输出完整 `X-OpenRag-Token` 或 `secret` 值。
- `write` 令牌具有完整读写权限，仅在必要时发放；日常检索使用 `read` 令牌即可。

### 2.11 与 OpenAPI 对齐

运行中服务可访问 **`/docs`**（Swagger UI）或 **`/redoc`**，在 **service** 和 **service-tokens** 标签下查看完整模型定义并试调（试调时在 security 输入框填入 `X-OpenRag-Token` 值）。

### 2.12 相关脚本与设计

- 数据库迁移脚本：`openrag/scripts/sql/2026-05-06-service-token-multi-workspace.sql`
- 设计背景：`docs/superpowers/specs/2026-05-06-service-token-multi-workspace-design.md`
