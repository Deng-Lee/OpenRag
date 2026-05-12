# Service Token API Design

**Date:** 2026-04-13（2026-04-14 修订：一令牌一工作区）  
**Topic:** 与用户 JWT 分离的服务 token、工作区权限及专用 REST 接口  
**Status:** Draft（待你审阅后标为 Approved）

## Overview

为外部系统集成增加 **服务 token** 鉴权能力：每个 token **在创建时绑定唯一一个工作区（项目空间）**，并带有该工作区上的 **read** / **write** 权限；**创建后工作区与权限不可更改**。所有对外集成能力通过 **`/service/v1` 前缀的专用接口** 暴露，与现有基于 JWT 的用户 API 在路由与依赖上完全分离。请求使用 **`X-OpenRag-Token`** 传递密钥；**每次请求通过路径参数携带全局唯一的工作区 `workspace_name`**。工作区内资源定位统一为 **以 `/` 开头的绝对逻辑路径**。

## Goals

1. 前端在工作区管理场景下可创建、列出、吊销 token；创建时选择 **当前工作区** 与 **read/write**（固定不可迁移到其他工作区）。
2. Token 格式：`sk-` + 密码学安全随机串；创建时展示完整密钥；列表默认脱敏，支持「显示明文」交互（见存储策略）。
3. 专用服务接口支持：目录全量树、一级子项、按路径取文档信息、上传/更新并触发解析任务、语义检索。
4. 服务接口鉴权仅依赖 token + 其绑定工作区，不依赖 `get_current_user`。

## Non-Goals

1. 不在本 spec 中定义与用户 JWT 共用同一批 URL 的双鉴权模式。
2. 不引入独立微服务或网关进程拆分（采用单体新路由树方案）。
3. 不对 token 做磁盘/字段级加密或哈希存储（见下文显式决策）；运维侧备份与访问控制不在本 spec 展开。

## Key Decisions（已确认）

| 项 | 决策 |
|----|------|
| 与用户 API 关系 | 单独一套 HTTP 接口（新前缀），不复用 JWT 路由。 |
| Token 传递 | 请求头 **`X-OpenRag-Token: sk-<secret>`**；不使用 `Authorization: Bearer` 传服务密钥。 |
| 工作区定位 | 路径参数 **`workspace_name`**；**`workspace.name` 全局唯一**。 |
| 路径语义 | **工作区内绝对逻辑路径**（非对象存储 URL、非 OpenRag 资源 URL）。 |
| 部署形态 | **方案一**：单体新增路由 + 复用领域服务（文件、任务、检索 pipeline）。 |
| Token 存储 | **数据库明文保存完整 `secret` 字符串**；列表/常规 API 不返回明文；前端默认星号脱敏，按需调「查看密钥」接口展示。 |
| 一令牌一工作区 | `service_tokens.workspace_id` + `permission`；无 junction 表；无「改绑工作区」API。 |

**安全备注（非功能约束）**：明文落库时，数据库与备份泄露即等同密钥泄露；应用 **禁止**在 info 级日志、访问日志、错误上报中输出完整 `X-OpenRag-Token` 或 `secret` 字段。

## Architecture

### 路由与依赖边界

- **前缀**：`/service/v1`（与用户域 `/api/...` 等并列，具体前缀以实现时 OpenRag 全局路由为准，但须固定且文档化）。
- **`/service/v1/**`**：仅使用 **`get_service_token_context`**（名称可微调），从 **`X-OpenRag-Token`** 解析 token 行并得到 **`workspace_id` + `permission`**；**禁止**依赖 `get_current_user`。
- **用户管理域**：创建/列出/吊销 token、获取明文 secret 等 **仅 JWT**；不接受服务 token 冒充登录态。

### 鉴权流程（服务请求）

1. 读取 `X-OpenRag-Token`，按 `secret` 匹配 `service_tokens` 且 `revoked_at IS NULL`。
2. 将路径中的 `workspace_name` 解析为 `Workspace`；不存在 → **404**。
3. 校验 token 的 **`workspace_id` 与当前 `Workspace.id` 一致**，且权限满足操作所需（写操作需 **write**；读操作 **read** 或 **write** 均可）。
4. 工作区不匹配或权限不足 → **403**；token 无效/缺失/已吊销 → **401**。

### 领域复用

- 逻辑路径与现有 `File`（及目录）模型对齐；若现有层尚无统一 canonical 路径，在服务层实现 **path ↔ 记录** 的单一映射规则，并与管理端展示一致。
- 上传/更新成功后 **与 JWT 上传相同** 的解析/索引任务入队逻辑，避免双套 pipeline。
- 语义检索复用现有向量检索实现；入口授权改为 token 上下文。

## Data Model（概念）

### `service_tokens`（单表）

| 字段 | 说明 |
|------|------|
| `id` | 主键 |
| `secret` | 完整明文 `sk-<random>` |
| `name` | 可选，管理用展示名 |
| `created_by_user_id` | 创建者 |
| `workspace_id` | FK → 唯一绑定的工作区 |
| `permission` | `read` \| `write`（写隐含读） |
| `created_at` / `revoked_at` | 吊销用软删除时间点 |
| `last_used_at` | 可选，v1 可不实现 |

**迁移**：若库中曾存在 `service_token_workspaces` 联结表，在业务库执行 `openrag/scripts/sql/2026-04-14-service-token-one-workspace-migrate.sql`（按联结表最小 id 取一条回填）；全新库使用 `2026-04-13-service-token.sql` 即可。

## Service API（`/service/v1`）

统一形态：**`/service/v1/workspaces/{workspace_name}/...`**，所有下列接口均需有效 **`X-OpenRag-Token`**。

### 1. 前缀下完整嵌套树

- **方法/路径**：`GET .../tree?path_prefix=<逻辑路径>`
- **说明**：`path_prefix` 默认 `/`。响应为嵌套 JSON：节点含 **canonical 绝对逻辑路径**（自根 `/`）、`kind`（`dir` \| `file`），目录含 `children`；文件可带 `size`、`mime`、`updated_at` 等与现有模型一致的子集。
- **边界**：须约定 **最大深度** 与/或 **最大节点数**，超出返回 **400** 或 **413**（实现选其一并文档化）；避免单次超大响应。

### 2. 仅一级子项

- **方法/路径**：`GET .../children?path=<目录路径>`
- **说明**：返回该目录下 **直接** 子目录与子文件的扁平列表。
- **边界**：单目录返回条数 **上限**（如 1000）；超出时 **400** 并提示缩小路径或后续版本分页。

### 3. 按路径取文档信息

- **方法/路径**：`GET .../documents/by-path?path=<文件路径>`
- **说明**：目标必须为 **文件**；否则 **404** 或 **400**（实现统一一种）。

### 4. 上传与更新（并触发解析）

- **新建**：`POST .../documents`（`multipart/form-data`：`file` + `path`（逻辑路径））。
  - 若路径已存在 → **409**，客户端应改用 `PUT`。
  - **v1**：若父目录路径不存在 → **400**（**不**自动递归创建父目录；若产品改为自动创建，须单独变更本 spec）。
- **覆盖**：`PUT .../documents/by-path?path=...`（multipart 或 raw body，与实现一致）。
- **响应**：含 `file_id`；可选 `task_id` 或与现网一致的任务标识。
- **权限**：需对该 `workspace_name` 为 token 绑定工作区且 **write**。

### 5. 语义检索

- **方法/路径**：`POST .../search`
- **Body**：`query`（必填）；`path_prefix`（可选，限制子树）。
- **权限**：该工作区 **read** 或 **write**（且须为 token 绑定工作区）。
- **响应**：与现网语义检索对齐的 hits（片段、分数、`source_path` 等字段名以实现为准）。

### 路径规范

- 仅接受 **以 `/` 开头的 canonical 绝对路径**。
- 含 `..`、非法 `//` 等：统一 **400**（或规范化为 canonical 二选一，须在实现与测试中固定一种）。

### HTTP 错误约定（汇总）

| 场景 | 状态码 |
|------|--------|
| 缺少/错误/已吊销 token | 401 |
| token 有效但请求的工作区不是其绑定工作区，或权限不足 | 403 |
| `workspace_name` 不存在 | 404 |
| 资源路径不存在或类型不符 | 404 或 400（全项目统一） |
| 违反路径/树大小/子项上限 | 400 或 413 |
| POST 文档路径已存在 | 409 |

## Admin API & Frontend（JWT）

- **创建**：`POST /workspaces/{workspace_id}/service-tokens`，body 含可选 `name`、必填语义字段 **`permission`**（`read`|`write`）；生成 `sk-` + 随机串，**明文写入** `service_tokens.secret`，并写入 **`workspace_id` = 路径中的工作区**（不可后续修改）。
- **列表**：`GET /workspaces/{workspace_id}/service-tokens`，仅返回 **`workspace_id` 等于该工作区** 的令牌；返回脱敏 `secret_preview` 与 `workspace_id`。
- **「显示密钥」**：`GET /service-tokens/{token_id}/secret`，仅 **创建者或系统管理员** 可拿到完整 `secret`。
- **吊销**：`DELETE /service-tokens/{token_id}` 设置 `revoked_at`；服务接口立即拒绝。

与当前 FastAPI 应用顶层路由一致的路径（无额外 `/api` 前缀时）：

| 操作 | 方法 | 路径 |
|------|------|------|
| 创建（需对该 `workspace_id` 具备写权限） | `POST` | `/workspaces/{workspace_id}/service-tokens` |
| 列表（该工作区成员即可读） | `GET` | `/workspaces/{workspace_id}/service-tokens` |
| 吊销 | `DELETE` | `/service-tokens/{token_id}` |
| 查看完整密钥 | `GET` | `/service-tokens/{token_id}/secret` |

前端：React 路由 **`/service-tokens`**（侧栏「服务令牌」）；创建前选择工作区，即令牌归属；无「绑定其他工作区」流程。构建与单测：`npm run build`、`npx vitest run`。

## Testing

- 后端：`openrag/pytest.ini` 通过 `python_files` 仅收集当前子集维护的契约用例（`test_service_*.py`、`test_service_api.py`、`test_service_tokens_admin.py`、`test_workspace_file_tree.py`、`test_files_api.py`）。运行：`cd OpenRag && python -m pytest tests/ -q`。
- 若浏览器请求 `GET /workspaces/{id}/service-tokens` 返回 **404**：说明运行中的 API 进程/容器仍是**未包含** `service_tokens_admin` 的旧代码；请在仓库根执行 **`docker compose -f docker/docker-compose.prod.yml build api`**（或等价）后重启 API，或在本机 **`cd OpenRag && pip install -e .`** 再启动 `run_api.py` / `uvicorn`。
- 若返回 **500** 且 JSON 为 `{"detail":"Database error occurred"}`：多为 **MySQL 尚未执行**建表脚本。全新部署执行 `openrag/scripts/sql/2026-04-13-service-token.sql`；由旧版联结表升级执行 `2026-04-14-service-token-one-workspace-migrate.sql`。排障：将环境变量 **`DEBUG=true`** 重启 API，同一错误响应中会附带简短 SQL 异常信息；API 日志中也会打印完整堆栈。
- 鉴权：`401` / `403` / 有效路径组合矩阵。
- 五类服务接口各至少一条契约测试；上传/更新断言解析任务入队（可 mock）。
- 管理端：无 JWT 拒绝；无写权限拒绝创建；列表响应不含无意泄露的 secret；`secret` 接口仅在授权下返回明文；令牌仅出现在其绑定工作区的列表中。
- 静态或集成断言：`/service/v1` 路由树不注册 `get_current_user` 依赖。

## Open Points（实现计划阶段细化即可）

- `tree` 的 **最大深度 / 最大节点数** 具体数值与配置方式。
- `children` 单页条数上限具体数值。
- 与现有 `File` 树 **路径拼接规则**（目录名、根节点）在代码中的唯一实现位置。

## Self-Review Checklist（本 spec）

- [x] 无「TBD」占位；开放项已收敛到 Open Points 小节。
- [x] 架构与「JWT / 服务 token 分离」一致。
- [x] 存储策略与用户明确选择一致，风险已标注。
- [x] 单实现计划可覆盖的范围；过大功能已列为 Non-Goals。
