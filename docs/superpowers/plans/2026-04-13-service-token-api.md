# Service Token API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 OpenRag 单体中实现与用户 JWT 完全分离的 `/service/v1` 服务接口（目录树、子项、文档元数据、上传/更新+解析任务、语义检索），以及 JWT 管理下的服务 token 与多工作区读写绑定；请求使用 `X-OpenRag-Token`，工作区用全局唯一的 `workspace_name` 路径参数定位。

**Architecture:** 新增 SQLAlchemy 模型 `ServiceToken` / `ServiceTokenWorkspace`（`secret` 明文存库）；`openrag.api.deps` 增加仅读请求头 `X-OpenRag-Token` 的 `get_service_token_context`；`openrag.api.service_api` 注册 `APIRouter(prefix="/service/v1")` 并在 `main.py` 挂载；业务复用 `File.uri` 作为工作区内逻辑路径、`validate_path`/`build_file_uri`、MinIO 写入与 `TaskService.create_task`（`user_id` 使用 `workspace.owner_id` 以满足非空外键与审计）；语义检索复用 `search_api` 中的 `_get_embedding_engine` / `_get_vector_store` / `_get_layer_store` 与 `RetrievalService.search(..., user_id=workspace.owner_id, workspace_id=...)`，必要时对结果按 `path_prefix` 过滤。

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Pydantic v2, MySQL（生产）/ SQLite（测试），现有 `openrag.api.files_api.validate_path`、`build_file_uri`、`MinioStorage`、`TaskService`、`RetrievalService`。

**Repo 根路径说明：** 下文 `openrag/` 指仓库内子目录 `openrag/`（含 `src/openrag` 与 `tests`），与顶层 `web/` 并列。

**Spec 对照：** `docs/superpowers/specs/2026-04-13-service-token-api-design.md`（已认可）。

---

## 文件结构总览（创建 / 修改）

| 路径 | 职责 |
|------|------|
| `openrag/src/openrag/models/service_token.py` | `ServiceToken`、`ServiceTokenWorkspace` 模型 |
| `openrag/src/openrag/models/__init__.py` | 导出新模型 |
| `openrag/src/openrag/models/workspace.py` | `Workspace.name` 增加全局 `unique=True`（与 spec 一致） |
| `openrag/scripts/sql/2026-04-13-service-token.sql` | 生产库 `CREATE TABLE` + `ALTER TABLE workspaces ADD UNIQUE`（若已有重复名需先数据清洗） |
| `openrag/src/openrag/api/deps.py` | `get_service_token_context`、`ServiceTokenContext` dataclass |
| `openrag/src/openrag/services/service_token_service.py` | 按 secret 解析 token、校验绑定与 read/write |
| `openrag/src/openrag/services/workspace_file_tree.py` | 在同一 `workspace_id` 下基于 `File` 行构建 `children`/`tree`、路径规范化与上限 |
| `openrag/src/openrag/services/file_ingest.py` | 从 `files_api.upload_file` 抽取「校验+MinIO+DB+任务」供 JWT 与 service 共用（可选但强烈推荐，避免重复） |
| `openrag/src/openrag/api/service_api.py` | 五类服务接口路由 |
| `openrag/src/openrag/api/service_tokens_admin.py` | JWT 下 token CRUD、绑定、吊销、`GET .../secret` |
| `openrag/src/openrag/api/main.py` | `include_router(service_router)`、`include_router(service_tokens_admin_router)` |
| `openrag/src/openrag/api/files_api.py` | 若抽取 `file_ingest`，改为调用该模块 |
| `web/src/services/api.ts` | 管理端 API 方法 |
| `web/src/pages/Permissions.tsx`（或新建 `ServiceTokens.tsx`） | 列表脱敏、创建弹窗、显示密钥按钮 |
| `openrag/tests/test_service_token_deps.py` | `get_service_token_context` 行为 |
| `openrag/tests/test_service_api.py` | 服务路由契约（TestClient + `dependency_overrides`） |

常量建议（在 `service_api.py` 或 `workspace_file_tree.py` 顶部）：`SERVICE_TOKEN_HEADER = "x-openrag-token"`，`TREE_MAX_NODES = 5000`，`TREE_MAX_DEPTH = 50`，`CHILDREN_LIMIT = 1000`。

---

### Task 1: 数据模型与 `workspace.name` 唯一

**Files:**

- Create: `openrag/src/openrag/models/service_token.py`
- Modify: `openrag/src/openrag/models/__init__.py`
- Modify: `openrag/src/openrag/models/workspace.py`（`name` 列 `unique=True`）
- Create: `openrag/scripts/sql/2026-04-13-service-token.sql`
- Test: `openrag/tests/test_models.py`（追加最小导入/表创建测试，或新建 `test_service_token_models.py`）

- [ ] **Step 1: 编写模型代码**

`openrag/src/openrag/models/service_token.py`：

```python
"""Service tokens for external API access (plaintext secret per product decision)."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openrag.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from openrag.models.user import User
    from openrag.models.workspace import Workspace


class ServiceTokenPermission(str, enum.Enum):
    read = "read"
    write = "write"


class ServiceToken(Base, TimestampMixin):
    __tablename__ = "service_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    secret: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    workspace_links: Mapped[List["ServiceTokenWorkspace"]] = relationship(
        back_populates="service_token", cascade="all, delete-orphan"
    )


class ServiceTokenWorkspace(Base):
    __tablename__ = "service_token_workspaces"
    __table_args__ = (
        UniqueConstraint("service_token_id", "workspace_id", name="uq_token_workspace"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    service_token_id: Mapped[int] = mapped_column(
        ForeignKey("service_tokens.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    permission: Mapped[str] = mapped_column(String(16), nullable=False)  # "read" | "write"

    service_token: Mapped["ServiceToken"] = relationship(back_populates="workspace_links")
    workspace: Mapped["Workspace"] = relationship()
```

在 `openrag/src/openrag/models/workspace.py` 中把 `name` 的 `mapped_column` 改为包含 `unique=True`（与 spec「全局唯一」一致）。

- [ ] **Step 2: 更新 `__init__.py`**

在 `openrag/src/openrag/models/__init__.py` 增加：

```python
from openrag.models.service_token import ServiceToken, ServiceTokenWorkspace, ServiceTokenPermission
```

并加入 `__all__`。

- [ ] **Step 3: 添加 SQL 迁移脚本（MySQL）**

`openrag/scripts/sql/2026-04-13-service-token.sql` 示例（按实际引擎调整类型）：

```sql
CREATE TABLE IF NOT EXISTS service_tokens (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  secret VARCHAR(256) NOT NULL UNIQUE,
  name VARCHAR(255) NULL,
  created_by_user_id BIGINT NOT NULL,
  revoked_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_st_user FOREIGN KEY (created_by_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS service_token_workspaces (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  service_token_id BIGINT NOT NULL,
  workspace_id BIGINT NOT NULL,
  permission VARCHAR(16) NOT NULL,
  CONSTRAINT fk_stw_token FOREIGN KEY (service_token_id) REFERENCES service_tokens(id) ON DELETE CASCADE,
  CONSTRAINT fk_stw_ws FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
  UNIQUE KEY uq_token_workspace (service_token_id, workspace_id)
);

-- 若已有重复 name，须先手工处理后再执行：
ALTER TABLE workspaces ADD UNIQUE KEY uq_workspaces_name (name);
```

- [ ] **Step 4: 运行测试**

```bash
cd OpenRag && pytest tests/test_models.py -v --tb=short
```

若新建 `test_service_token_models.py`，则：

```bash
cd OpenRag && pytest tests/test_service_token_models.py -v --tb=short
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/models/service_token.py openrag/src/openrag/models/__init__.py openrag/src/openrag/models/workspace.py openrag/scripts/sql/2026-04-13-service-token.sql openrag/tests/test_service_token_models.py
git commit -m "feat(db): add service token models and workspace name uniqueness"
```

---

### Task 2: `get_service_token_context` 依赖

**Files:**

- Modify: `openrag/src/openrag/api/deps.py`
- Create: `openrag/tests/test_service_token_deps.py`

- [ ] **Step 1: 编写失败测试**

`openrag/tests/test_service_token_deps.py`：

```python
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models.base import Base
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.service_token import ServiceToken, ServiceTokenWorkspace
from openrag.security import hash_password


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def test_get_service_token_context_ok(db):
    from openrag.api.deps import get_service_token_context

    u = User(
        username="u1", email="u1@e.com", password_hash=hash_password("x"), full_name="U", is_active=True
    )
    db.add(u)
    db.commit()
    ws = Workspace(name="wsuniq", slug="wsuniq", owner_id=u.id)
    db.add(ws)
    db.commit()
    tok = ServiceToken(secret="sk-testsecret", created_by_user_id=u.id)
    db.add(tok)
    db.commit()
    db.add(ServiceTokenWorkspace(service_token_id=tok.id, workspace_id=ws.id, permission="read"))
    db.commit()

    ctx = get_service_token_context(x_openrag_token="sk-testsecret", db=db)
    assert ctx.token_id == tok.id
    assert ws.id in ctx.permission_by_workspace_id
    assert ctx.permission_by_workspace_id[ws.id] == "read"
```

此时 `get_service_token_context` 尚未实现，运行：

```bash
cd OpenRag && pytest tests/test_service_token_deps.py::test_get_service_token_context_ok -v
```

Expected: FAIL（ImportError 或 not defined）

- [ ] **Step 2: 实现依赖（最小可用）**

在 `openrag/src/openrag/api/deps.py` 增加（示意，需与项目 import 风格一致）：

```python
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import Header

from openrag.models.service_token import ServiceToken, ServiceTokenWorkspace


@dataclass
class ServiceTokenContext:
    token_id: int
    permission_by_workspace_id: Dict[int, str]  # "read" | "write"


def get_service_token_context(
    db: Session = Depends(get_db),
    x_openrag_token: Optional[str] = Header(default=None, alias="X-OpenRag-Token"),
) -> ServiceTokenContext:
    if not x_openrag_token or not x_openrag_token.startswith("sk-"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing service token")

    row = db.query(ServiceToken).filter(ServiceToken.secret == x_openrag_token, ServiceToken.revoked_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing service token")

    links = db.query(ServiceTokenWorkspace).filter(ServiceTokenWorkspace.service_token_id == row.id).all()
    perm: Dict[int, str] = {}
    for link in links:
        existing = perm.get(link.workspace_id)
        newp = link.permission
        if existing is None:
            perm[link.workspace_id] = newp
        elif existing == "write" or newp == "write":
            perm[link.workspace_id] = "write"
        else:
            perm[link.workspace_id] = "read"
    return ServiceTokenContext(token_id=row.id, permission_by_workspace_id=perm)
```

注意：`get_service_token_context` 若作为 **同步** 函数被 FastAPI 调用，签名中 `db` 与 `Header` 的顺序需符合 FastAPI 规则；若与现有全异步风格不一致，可改为 `async def` 并在测试中通过直接传 `db=` 调用（如上测试所示，绕过 FastAPI）。

为测试通过，在测试中 **不要**依赖 Header，直接调用 `get_service_token_context(x_openrag_token="sk-testsecret", db=db)` 需要把函数改成支持可选的 `db` 注入，或使用 `Request` 夹具；更简单做法：把解析逻辑放到 `openrag.services.service_token_service.resolve_context(db, raw_token)`，`deps` 里一行调用。

调整 Step 2 为：实现 `openrag/src/openrag/services/service_token_service.py`：

```python
def resolve_service_token_context(db: Session, raw_token: str | None) -> ServiceTokenContext:
    if not raw_token or not raw_token.startswith("sk-"):
        raise HTTPException(status_code=401, detail="Invalid or missing service token")
    ...
```

`deps.py`：

```python
def get_service_token_context(
    db: Session = Depends(get_db),
    x_openrag_token: Optional[str] = Header(default=None, alias="X-OpenRag-Token"),
) -> ServiceTokenContext:
    return resolve_service_token_context(db, x_openrag_token)
```

测试改为 `from openrag.services.service_token_service import resolve_service_token_context` 并调用 `resolve_service_token_context(db, "sk-testsecret")`。

- [ ] **Step 3: 补充吊销与错误 token 测试**

同文件增加：

```python
def test_resolve_revoked(db):
    from openrag.services.service_token_service import resolve_service_token_context
    # 创建 token 并设置 revoked_at 非空，期望 401
    ...
```

- [ ] **Step 4: 运行测试**

```bash
cd OpenRag && pytest tests/test_service_token_deps.py -v --tb=short
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/api/deps.py openrag/src/openrag/services/service_token_service.py openrag/tests/test_service_token_deps.py
git commit -m "feat(api): service token context resolution from X-OpenRag-Token"
```

---

### Task 3: 工作区解析与权限 helper

**Files:**

- Create: `openrag/src/openrag/services/service_token_service.py`（继续扩展）或 `openrag/src/openrag/api/service_deps.py`

在 `service_token_service.py` 增加：

```python
def require_workspace_for_name(db: Session, workspace_name: str) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.name == workspace_name).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


def assert_token_workspace_permission(
    ctx: ServiceTokenContext,
    workspace_id: int,
    need: str,  # "read" | "write"
) -> None:
    perm = ctx.permission_by_workspace_id.get(workspace_id)
    if perm is None:
        raise HTTPException(status_code=403, detail="Token not authorized for this workspace")
    if need == "read" and perm not in ("read", "write"):
        raise HTTPException(status_code=403, detail="Read permission required")
    if need == "write" and perm != "write":
        raise HTTPException(status_code=403, detail="Write permission required")
```

- [ ] **Step 1–3:** 实现上述函数 + 单元测试（sqlite 内存库，同 Task 2 fixture）。
- [ ] **Step 4:** `pytest tests/test_service_token_workspace.py -v`
- [ ] **Step 5:** `git commit -m "feat(service-token): workspace name resolve and permission checks"`

---

### Task 4: `workspace_file_tree`（tree / children / by-path）

**Files:**

- Create: `openrag/src/openrag/services/workspace_file_tree.py`
- Test: `openrag/tests/test_workspace_file_tree.py`

实现要点：

- 输入 `workspace_id`、`path`（已 `validate_path`）。
- **children**：`File.workspace_id == ws` 且 `parent_id` 指向 uri 等于该路径的目录行；若无目录行，可根据 `uri` 前缀推导「虚拟父目录」——与现网一致优先：仅当 DB 中已有目录 `File.is_directory` 与 `uri` 匹配时列出子项；否则返回 404（与 spec「父目录不存在则上传 400」一致，浏览侧找不到父目录则 404）。
- **tree**：在 `path_prefix` 下递归构建，应用 `TREE_MAX_DEPTH` / `TREE_MAX_NODES`，超出 `HTTPException(400, detail="...")`。
- **by-path**：`File.workspace_id` + `File.uri == path` + `is_directory is False`。

- [ ] **Step 1:** 写 `test_children_returns_direct_files`（插入目录 `/a`、文件 `/a/b.txt`）。
- [ ] **Step 2:** 实现 `list_children(db, workspace_id, dir_path)`。
- [ ] **Step 3–5:** tree、by-path 测试与实现，`pytest tests/test_workspace_file_tree.py -v`，commit。

---

### Task 5: `service_api` 路由（读路径）

**Files:**

- Create: `openrag/src/openrag/api/service_api.py`
- Modify: `openrag/src/openrag/api/main.py`
- Test: `openrag/tests/test_service_api.py`

```python
router = APIRouter(prefix="/service/v1", tags=["service"])

@router.get("/workspaces/{workspace_name}/tree")
async def service_tree(...):
    ...

@router.get("/workspaces/{workspace_name}/children")
async def service_children(...):
    ...

@router.get("/workspaces/{workspace_name}/documents/by-path")
async def service_document_by_path(...):
    ...
```

每个 handler：`ctx = Depends(get_service_token_context)` → `ws = require_workspace_for_name(...)` → `assert_token_workspace_permission(ctx, ws.id, "read")` → 调用 `workspace_file_tree`。

TestClient：`app.dependency_overrides[get_db]` 与内存 SQLite；插入数据后请求带 `headers={"X-OpenRag-Token": "sk-x"}`。

- [ ] **Step 5:** `git commit -m "feat(api): service read endpoints tree children document meta"`

---

### Task 6: 上传与更新（写路径）+ 任务

**Files:**

- Create: `openrag/src/openrag/services/file_ingest.py`（从 `files_api.upload_file` 抽取）
- Modify: `openrag/src/openrag/api/files_api.py`
- Modify: `openrag/src/openrag/api/service_api.py`

`file_ingest.ingest_uploaded_bytes(...)` 参数至少包含：`db`, `workspace`, `logical_parent_path`, `filename`, `bytes`, `content_type`, `parser_type`, `owner_user_id`（JWT 用 `current_user.id`，service 用 `workspace.owner_id`），返回 `(file_record, task_record|None)`。保持与现网一致的 `ALLOWED_MIME_TYPES` 与 `TaskService.create_task` 分支。

`service_api`：

- `POST .../documents`：`Form(path)`, `File(file)` → 校验父目录存在 → 若 `(uri, workspace_id)` 已存在且为文件 → **409**。
- `PUT .../documents/by-path`：覆盖 MinIO 对象与 `File` 大小/mime，重置处理状态并入队新 `process_document` 任务（与 reprocess 类似）。

- [ ] **pytest** `tests/test_service_api.py` 中 `test_service_upload_creates_task`（mock `TaskService` 或断言 DB 中 `Task` 行）。
- [ ] **Commit** `feat(api): service document upload put and shared ingest helper`

---

### Task 7: 语义检索 `POST .../search`

**Files:**

- Modify: `openrag/src/openrag/api/service_api.py`
- 可选 Modify: `openrag/src/openrag/api/search_api.py`（若抽取公共函数 `run_search(db, user_id, request: SearchRequest) -> SearchResponse`）

实现：在 `service_api` 内构造 `SearchRequest(query=..., workspace_id=ws.id, ...)`，调用与 `search_api` 相同的 `_execute_search`（需将 `_execute_search` 抽到 `openrag.api.search_common` 或在 `service_api` 中复制最小导入链——**优先抽取**避免重复）。

`user_id` 传 `ws.owner_id`（与 `RetrievalService._accessible_file_ids` 在 `workspace_id` 固定时行为一致）。

若支持 `path_prefix`：在返回的 `results` 上过滤 `uri` 或 `filename` 前缀（与 spec 一致即可）。

- [ ] **测试**：mock `RetrievalService.search` 返回一条 hit，断言 200。
- [ ] **Commit** `feat(api): service semantic search endpoint`

---

### Task 8: JWT 管理端 `service_tokens_admin`

**Files:**

- Create: `openrag/src/openrag/api/service_tokens_admin.py`
- Modify: `openrag/src/openrag/api/main.py`

建议路由（与现有风格对齐，可微调）：

- `POST /workspaces/{workspace_id}/service-tokens` body: `{ "name": "optional" }` — 创建 `sk-`+`secrets.token_urlsafe(32)`，并插入 `ServiceTokenWorkspace(workspace_id, permission="write")` 作为首绑定（调用者须对该 workspace `write`：`get_workspace_admin`）。
- `GET /workspaces/{workspace_id}/service-tokens` — 返回绑定到该 workspace 的 token 列表项：`id`, `name`, `secret_preview`（如 `sk-****` + 末 4 位），**不含**完整 secret。
- `POST /service-tokens/{token_id}/bindings` body: `{ "workspace_id": n, "permission": "read"|"write" }` — 仅创建者可或 workspace admin（按产品定一种，**推荐**：系统管理员或 **任一已绑定 workspace 的 write 成员** 可管理绑定）。
- `DELETE /service-tokens/{token_id}` — 吊销 `revoked_at=now()`。
- `GET /service-tokens/{token_id}/secret` — 返回 `{ "secret": "<full>" }`，需 **创建者或 workspace owner 或系统管理员**（择一写死并测）。

生成 secret：

```python
import secrets
secret = "sk-" + secrets.token_urlsafe(32)
```

- [ ] **测试** `tests/test_service_tokens_admin.py`：无 JWT 401；无写权限 403；`GET secret` 权限。
- [ ] **Commit** `feat(api): JWT admin endpoints for service tokens`

---

### Task 9: 前端管理 UI

**Files:**

- Modify: `web/src/services/api.ts`
- Modify: `web/src/pages/Permissions.tsx` 或新建页面并在 `App.tsx` 注册路由

行为：选择 workspace →「API Token」表格 → 创建（弹窗展示一次 secret）→ 复制 → 列表脱敏 →「显示密钥」调用 `GET .../secret` 再弹窗。

- [ ] **手动验证**：浏览器走一遍创建/显示/吊销。
- [ ] **Commit** `feat(web): service token management UI`

---

### Task 10: 全量回归与文档

- [ ] **运行** `cd OpenRag && pytest -q` 与 `cd web && npm test`（若项目已有 CI 命令则与之对齐）。
- [ ] **更新** `openrag/README.md` 或 `docker/QUICKSTART.md` 中一节「服务 API」：`X-OpenRag-Token`、`/service/v1` 示例 curl（**curl 示例中不要用真实生产密钥**）。
- [ ] **Commit** `docs: document service token API usage`

---

## Spec 覆盖自检

| Spec 章节 | 对应 Task |
|-----------|-----------|
| 分离路由与 Header | Task 2, 5, `main.py` |
| 五类服务接口 | Task 4–7 |
| 明文存储 + 管理端 secret 接口 + 前端脱敏 | Task 1, 8, 9 |
| workspace_name 全局唯一 | Task 1 `workspace.name` + SQL |
| 权限 read/write、401/403/404/409 | Task 2–6 |
| 树/子项上限 | Task 4 |
| 禁止日志打印 token | 在 `file_ingest` / `service_api` 代码评审中显式检查；勿 `logger.info(request.headers)` |

## 计划自检（无占位）

- 已避免「TBD / 稍后实现」；开放点仅为 **Admin 绑定编辑的授权模型** 在 Task 8 中给出了可选推荐，实施时选一种并写入测试断言。
- `ServiceTokenContext` / `resolve_service_token_context` 命名在 Task 2 与 Task 3 中一致。

---

## Execution handoff

**计划已保存到** `docs/superpowers/plans/2026-04-13-service-token-api.md`。

**两种执行方式：**

1. **Subagent-Driven（推荐）** — 每个 Task 派生子代理执行，任务间人工快速 review。需配合 **superpowers:subagent-driven-development**。
2. **Inline Execution** — 本会话内按 Task 顺序执行，配合 **superpowers:executing-plans** 与检查点。

你更倾向哪一种？
