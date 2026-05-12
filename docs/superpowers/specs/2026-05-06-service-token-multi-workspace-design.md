---
name: Service Token 多项目空间授权
description: 将 Service Token 从单 workspace 绑定改为多 workspace 授权，每个 workspace 拥有独立权限级别
type: project
---

# Service Token 多项目空间授权设计

## 背景

当前每个 `ServiceToken` 通过 `workspace_id`（NOT NULL FK）绑定到唯一一个 workspace，拥有单一 `permission` 字段（`read` 或 `write`）。授权断言 `assert_token_workspace_permission(ctx, workspace.id, need)` 严格校验 `ctx.workspace_id == workspace.id`。这意味着外部系统若需访问多个 workspace，必须为每个 workspace 创建单独的 token。

本设计将模型改为：单个 token 可授权访问多个 workspace，每个 workspace 拥有独立的权限级别。

## 1. 数据模型

### ServiceToken 表变更

从 `service_tokens` 表移除两个字段：

| 移除字段 | 原因 |
|---|---|
| `workspace_id` (NOT NULL FK → workspaces.id) | 由关联表替代 |
| `permission` (NOT NULL String(16)) | 由关联表替代 |

保留字段：`id, secret, name, created_by_user_id, revoked_at, created_at, updated_at`。

### 新增关联表：`service_token_workspaces`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK autoincrement | 主键 |
| token_id | int FK → service_tokens.id | ON DELETE CASCADE |
| workspace_id | int FK → workspaces.id | ON DELETE CASCADE |
| permission | String(16) NOT NULL | `"read"` 或 `"write"` |
| created_at | DateTime | 绑定时间 |

**约束**：`UniqueConstraint(token_id, workspace_id)` — 同一个 token 对同一个 workspace 只能有一条绑定。

**索引**：`idx_stw_token` 在 `token_id`，`idx_stw_workspace` 在 `workspace_id`。

### ORM 模型：`ServiceTokenWorkspace`

```python
class ServiceTokenWorkspace(Base, TimestampMixin):
    __tablename__ = "service_token_workspaces"
    __table_args__ = (UniqueConstraint("token_id", "workspace_id", name="uq_stw_token_workspace"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("service_tokens.id", ondelete="CASCADE"))
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    permission: Mapped[str] = mapped_column(String(16), nullable=False)

    token: Mapped["ServiceToken"] = relationship(backref="workspace_bindings")
    workspace: Mapped["Workspace"] = relationship()
```

### ServiceTokenContext 变更

将单 workspace 上下文替换为绑定列表：

```python
@dataclass(frozen=True)
class TokenWorkspaceBinding:
    workspace_id: int
    permission: str  # "read" | "write"

@dataclass(frozen=True)
class ServiceTokenContext:
    token_id: int
    bindings: list[TokenWorkspaceBinding]
```

### 迁移脚本

自动将现有 `ServiceToken.workspace_id + permission` 转为 `service_token_workspaces` 记录，然后 ALTER TABLE 删除这两个字段：

```sql
-- 步骤1：创建关联表（通过 ORM 迁移完成）
-- 步骤2：从现有数据填充
INSERT INTO service_token_workspaces (token_id, workspace_id, permission, created_at)
SELECT id, workspace_id, permission, created_at FROM service_tokens WHERE workspace_id IS NOT NULL AND revoked_at IS NULL;

-- 步骤3：迁移已撤销的 token 以保留历史记录
INSERT INTO service_token_workspaces (token_id, workspace_id, permission, created_at)
SELECT id, workspace_id, permission, created_at FROM service_tokens WHERE workspace_id IS NOT NULL AND revoked_at IS NOT NULL;

-- 步骤4：删除旧字段
ALTER TABLE service_tokens DROP COLUMN workspace_id;
ALTER TABLE service_tokens DROP COLUMN permission;
```

## 2. 管理 API（JWT 认证）

### 创建 token — `POST /service-tokens`

请求体：
```json
{
  "name": "可选标签",
  "workspaces": [
    {"workspace_id": 1, "permission": "write"},
    {"workspace_id": 2, "permission": "read"}
  ]
}
```

- `workspaces` 数组至少包含 1 个条目
- 校验：当前用户对每个 `workspace_id` 必须拥有 `write` 或 `admin` 角色
- 返回：token 详情 + 完整 `secret`（仅显示一次）

### 列表 token — `GET /service-tokens`

查询参数：
- `workspace_id`（可选）：过滤出绑定到该 workspace 的 token

返回：所有可见 token 及其 workspace 绑定列表（每个绑定包含 workspace 名称 + 权限）。

### 查看 secret — `GET /service-tokens/{token_id}/secret`

不变。仅 token 创建者或系统 admin 可访问，且仅对未撤销的 token。

### 修改 workspace 绑定 — `PATCH /service-tokens/{token_id}/workspaces`

请求体：
```json
{
  "add": [{"workspace_id": 3, "permission": "read"}],
  "update": [{"workspace_id": 2, "permission": "write"}],
  "remove": [{"workspace_id": 1}]
}
```

- `add` 和 `update` 的校验：当前用户对每个目标 workspace 必须拥有 `write` 或 `admin` 角色
- `remove` 无需 workspace 权限校验（移除绑定只缩减访问范围，不扩大权限）
- `add` 创建新的 `service_token_workspaces` 记录；`update` 修改已有绑定的 `permission`；`remove` 删除绑定
- Token 必须未被撤销
- 仅 token 创建者或系统 admin 可修改绑定

### 撤销 token — `DELETE /service-tokens/{token_id}`

不变。通过设置 `revoked_at` 实现软撤销。绑定记录保留在 `service_token_workspaces` 中用于审计历史。服务 API 的 `resolve_service_token_context` 已通过 `revoked_at IS NULL` 过滤，撤销的 token 不会被解析，无论绑定是否存在。

### 移除的路径

以下路由废弃并移除：
- `POST /workspaces/{workspace_id}/service-tokens`
- `GET /workspaces/{workspace_id}/service-tokens`
- `PATCH /workspaces/{workspace_id}/service-tokens/{token_id}`

## 3. 服务 API（Token 认证）

### 授权逻辑变更

`assert_token_workspace_permission(ctx, workspace_id, need)` 从：

```python
# 旧逻辑：单 workspace 绑定
if ctx.workspace_id != workspace_id:
    raise HTTPException(403)
if not permission_satisfied(ctx.permission, need):
    raise HTTPException(403)
```

改为：

```python
# 新逻辑：在绑定列表中查找
binding = next((b for b in ctx.bindings if b.workspace_id == workspace_id), None)
if binding is None:
    raise HTTPException(403, "Token not authorized for this workspace")
if not permission_satisfied(binding.permission, need):
    raise HTTPException(403, "Token permission insufficient")
```

其中 `permission_satisfied(binding.permission, need)` 仍遵循：`write` 可满足 `read` 请求；`read` 不能满足 `write` 请求。

### `GET /service/v1/workspaces` 端点

返回内容从单条记录改为完整绑定列表：

```json
{
  "workspaces": [
    {"name": "project-x", "slug": "project-x", "permission": "write"},
    {"name": "project-y", "slug": "project-y", "permission": "read"}
  ]
}
```

### 其他端点不变

所有 `/service/v1/{workspace_name}/...` 端点（tree、children、document-by-path、upload/update、search）保持 URL 结构不变，仅授权检查逻辑按上述方式更新。

### Token 解析

`resolve_service_token_context` 现在同时查询 `service_tokens` 和 `service_token_workspaces`：

```python
def resolve_service_token_context(db: Session, secret: str) -> ServiceTokenContext:
    token = db.query(ServiceToken).filter_by(secret=secret, revoked_at=None).first()
    if not token:
        raise InvalidTokenError()
    bindings = db.query(ServiceTokenWorkspace).filter_by(token_id=token.id).all()
    if not bindings:
        raise HTTPException(403, "Token has no workspace bindings")
    return ServiceTokenContext(
        token_id=token.id,
        bindings=[TokenWorkspaceBinding(ws.workspace_id, ws.permission) for ws in bindings]
    )
```

## 4. 前端 UI

### ServiceTokens 页面变更

- **移除**页面顶部的 workspace 选择器（token 不再属于单个 workspace）
- **Token 列表**：新增"关联 workspace"列，每个绑定显示为标签（workspace 名称 + 权限徽章）
- **创建 token 弹窗**：将单 workspace 下拉框替换为多选组件。每个选中的 workspace 可独立设置 read/write 权限下拉框
- **管理 workspace 绑定**：每个 token 行新增"管理"操作按钮，弹窗中包含：
  - 当前绑定以可编辑行展示（workspace 名称 + 权限下拉框 + 移除按钮）
  - "添加 workspace"按钮，支持搜索选择 workspace 并设定权限
- **查看 secret / 撤销**：不变

### 路由与访问

`/service-tokens` 路由保持仅 admin 可访问（ProtectedRoute + AdminRoute）。

## 5. 错误处理

| 场景 | HTTP 状态码 | 返回信息 |
|---|---|---|
| 无绑定的 token 访问任何 workspace | 403 | `"Token has no workspace bindings"` |
| 绑定存在但权限不足 | 403 | `"Token permission insufficient for this workspace"` |
| Token 未绑定到请求的 workspace | 403 | `"Token not authorized for this workspace"` |
| 用户在添加/更新绑定时缺少目标 workspace 权限 | 403 | `"You do not have permission to authorize access to this workspace"` |
| 创建 token 时 workspaces 数组为空 | 400 | `"At least one workspace binding is required"` |
| 创建请求中存在重复 workspace_id | 400 | `"Duplicate workspace binding"` |
| 添加已存在的 workspace 绑定 | 409 | `"Token already has a binding for this workspace"` |
| 修改已撤销 token 的绑定 | 400 | `"Cannot modify bindings on a revoked token"` |

## 6. 测试

### 需更新的单元测试

- `test_service_token_deps.py`：更新 token 解析测试以返回绑定列表
- `test_service_token_workspace.py`：更新 workspace 名称解析和权限断言以适配绑定列表查找
- `test_service_tokens_admin.py`：更新 CRUD 测试以适配新 API 路径和绑定操作

### 需新增的测试

- 多 workspace token 创建（一次请求中多个绑定）
- 混合权限检查（workspace A 为 write，workspace B 为 read）
- 绑定修改：添加、更新、移除
- 校验：用户缺少目标 workspace 权限
- 校验：请求中存在重复 workspace_id
- 校验：workspaces 数组为空
- 服务 API：多绑定 token 访问不同 workspace
- 服务 API：无绑定 token（应失败）
- 迁移：现有单 workspace token 正确迁移

## 7. 迁移策略

1. 通过 Alembic 迁移创建 `service_token_workspaces` 表
2. 执行数据迁移：`INSERT INTO service_token_workspaces ... SELECT id, workspace_id, permission, created_at FROM service_tokens`
3. 从 `service_tokens` 表删除 `workspace_id` 和 `permission` 列
4. 更新 ORM 模型、服务层和 API 路由
5. 移除旧路由 `/workspaces/{workspace_id}/service-tokens`
6. 更新前端 ServiceTokens 页面

迁移必须作为单个 Alembic 迁移执行以确保原子性。