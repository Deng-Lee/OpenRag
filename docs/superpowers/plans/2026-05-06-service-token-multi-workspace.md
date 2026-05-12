# Service Token 多项目空间授权 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Service Token 从单 workspace 绑定改为多 workspace 授权，每个 workspace 拥有独立权限级别。

**Architecture:** 新增 `service_token_workspaces` 关联表替代 `ServiceToken` 上的 `workspace_id` + `permission` 字段。`ServiceTokenContext` 改为持有绑定列表，授权检查在列表中查找匹配 workspace。管理 API 从 workspace-nested 路径改为 flat 路径 + 绑定管理端点。前端去掉 workspace 选择器，改为多选绑定 UI。

**Tech Stack:** Python/SQLAlchemy (ORM), FastAPI (API), Pydantic (validation), React + Ant Design (frontend), TypeScript, i18next (i18n)

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `openrag/src/openrag/models/service_token_workspace.py` | ServiceTokenWorkspace ORM 模型（关联表） |
| Modify | `openrag/src/openrag/models/service_token.py` | 移除 `workspace_id`、`permission`、`workspace` relationship |
| Modify | `openrag/src/openrag/models/__init__.py` | 导出 `ServiceTokenWorkspace` |
| Modify | `openrag/src/openrag/services/service_token_service.py` | 新 `ServiceTokenContext` + `TokenWorkspaceBinding` + 更新 resolver/permission |
| Modify | `openrag/src/openrag/api/service_tokens_admin.py` | 替换 workspace-nested 路径为 flat 路径 + 绑定管理 |
| Modify | `openrag/src/openrag/api/service_api.py` | `service_list_workspaces` 返回绑定列表 |
| Create | `openrag/scripts/sql/2026-05-06-service-token-multi-workspace.sql` | 迁移 SQL |
| Modify | `web/src/types/index.ts` | 更新 ServiceToken 相关类型 |
| Modify | `web/src/services/api.ts` | 更新 serviceTokensAPI |
| Modify | `web/src/pages/ServiceTokens.tsx` | 多 workspace 绑定 UI |
| Modify | `web/src/i18n/locales/en.json` | 新增/修改 service token i18n |
| Modify | `web/src/i18n/locales/zh.json` | 新增/修改 service token i18n |
| Modify | `openrag/tests/test_service_token_models.py` | 更新模型测试 |
| Modify | `openrag/tests/test_service_token_deps.py` | 更新 resolver 测试 |
| Modify | `openrag/tests/test_service_token_workspace.py` | 更新权限检查测试 |
| Modify | `openrag/tests/test_service_tokens_admin.py` | 重写管理 API 测试 |

---

### Task 1: ServiceTokenWorkspace ORM 模型

**Files:**
- Create: `openrag/src/openrag/models/service_token_workspace.py`
- Modify: `openrag/src/openrag/models/__init__.py`
- Test: `openrag/tests/test_service_token_models.py`

- [ ] **Step 1: 写 ServiceTokenWorkspace 模型的失败测试**

在 `test_service_token_models.py` 中新增测试（追加在文件末尾）：

```python
def test_service_token_workspace_binding(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    from openrag.models import ServiceTokenWorkspace

    token = ServiceToken(
        secret="sk-binding-test",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    binding = ServiceTokenWorkspace(
        token_id=token.id,
        workspace_id=workspace.id,
        permission="read",
    )
    db_session.add(binding)
    db_session.commit()
    db_session.refresh(binding)

    assert binding.token_id == token.id
    assert binding.workspace_id == workspace.id
    assert binding.permission == "read"


def test_service_token_workspace_unique_constraint(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    from openrag.models import ServiceTokenWorkspace
    from sqlalchemy.exc import IntegrityError

    token = ServiceToken(
        secret="sk-unique-binding",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    b1 = ServiceTokenWorkspace(token_id=token.id, workspace_id=workspace.id, permission="read")
    b2 = ServiceTokenWorkspace(token_id=token.id, workspace_id=workspace.id, permission="write")
    db_session.add_all([b1, b2])
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_service_token_workspace_cascade_delete(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    from openrag.models import ServiceTokenWorkspace

    token = ServiceToken(
        secret="sk-cascade-delete",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    binding = ServiceTokenWorkspace(
        token_id=token.id,
        workspace_id=workspace.id,
        permission="write",
    )
    db_session.add(binding)
    db_session.commit()

    db_session.delete(token)
    db_session.commit()

    assert db_session.query(ServiceTokenWorkspace).filter_by(token_id=token.id).count() == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd OpenRag && python -m pytest tests/test_service_token_models.py::test_service_token_workspace_binding -v`
Expected: FAIL — `ImportError: cannot import name 'ServiceTokenWorkspace'`

- [ ] **Step 3: 创建 ServiceTokenWorkspace ORM 模型**

创建 `openrag/src/openrag/models/service_token_workspace.py`：

```python
"""Service token → workspace binding model (multi-workspace authorization)."""

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openrag.models.base import Base, TimestampMixin


class ServiceTokenWorkspace(Base, TimestampMixin):
    __tablename__ = "service_token_workspaces"
    __table_args__ = (
        UniqueConstraint("token_id", "workspace_id", name="uq_stw_token_workspace"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        ForeignKey("service_tokens.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    permission: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="read or write",
    )

    token: Mapped["ServiceToken"] = relationship(backref="workspace_bindings")
    workspace: Mapped["Workspace"] = relationship()

    def __repr__(self) -> str:
        return f"<ServiceTokenWorkspace(token_id={self.token_id}, workspace_id={self.workspace_id}, permission={self.permission})>"
```

- [ ] **Step 4: 更新 __init__.py 导出**

在 `openrag/src/openrag/models/__init__.py` 中：
- 添加 import: `from openrag.models.service_token_workspace import ServiceTokenWorkspace`
- 添加到 `__all__`: `"ServiceTokenWorkspace"`

- [ ] **Step 5: 运行测试确认通过**

Run: `cd OpenRag && python -m pytest tests/test_service_token_models.py -v`
Expected: PASS（新旧测试全部通过）

- [ ] **Step 6: Commit**

```bash
git add openrag/src/openrag/models/service_token_workspace.py openrag/src/openrag/models/__init__.py openrag/tests/test_service_token_models.py
git commit -m "feat(models): add ServiceTokenWorkspace junction table model"
```

---

### Task 2: 更新 ServiceTokenContext 和服务层

**Files:**
- Modify: `openrag/src/openrag/services/service_token_service.py`
- Test: `openrag/tests/test_service_token_deps.py`
- Test: `openrag/tests/test_service_token_workspace.py`

- [ ] **Step 1: 写 ServiceTokenContext 绑定列表的失败测试**

在 `test_service_token_deps.py` 中修改现有测试并新增测试：

```python
from openrag.models import ServiceTokenWorkspace


def test_resolve_valid_token_returns_binding_context(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    token = ServiceToken(
        secret="sk-abc",
        name="api",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    binding = ServiceTokenWorkspace(
        token_id=token.id,
        workspace_id=workspace.id,
        permission="read",
    )
    db_session.add(binding)
    db_session.commit()

    ctx = resolve_service_token_context(db_session, "sk-abc")
    assert isinstance(ctx, ServiceTokenContext)
    assert ctx.token_id == token.id
    assert len(ctx.bindings) == 1
    assert ctx.bindings[0].workspace_id == workspace.id
    assert ctx.bindings[0].permission == "read"


def test_resolve_multi_workspace_token(
    db_session: Session, owner: User, workspace: Workspace
) -> None:
    ws2 = Workspace(name="WS2", slug="ws2", owner_id=owner.id)
    db_session.add(ws2)
    db_session.commit()
    db_session.refresh(ws2)

    token = ServiceToken(
        secret="sk-multi",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    db_session.add_all([
        ServiceTokenWorkspace(token_id=token.id, workspace_id=workspace.id, permission="write"),
        ServiceTokenWorkspace(token_id=token.id, workspace_id=ws2.id, permission="read"),
    ])
    db_session.commit()

    ctx = resolve_service_token_context(db_session, "sk-multi")
    assert len(ctx.bindings) == 2
    ws_ids = {b.workspace_id for b in ctx.bindings}
    assert ws_ids == {workspace.id, ws2.id}


def test_resolve_token_no_bindings_raises_403(
    db_session: Session, owner: User
) -> None:
    token = ServiceToken(
        secret="sk-no-bindings",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        resolve_service_token_context(db_session, "sk-no-bindings")
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc.value.detail == "Token has no workspace bindings"
```

删除旧的 `test_resolve_valid_token_returns_context`（使用了旧的 `workspace_id/permission` 模型）。

- [ ] **Step 2: 写绑定列表权限检查的失败测试**

在 `test_service_token_workspace.py` 中修改现有测试：

```python
from openrag.services.service_token_service import (
    ServiceTokenContext,
    TokenWorkspaceBinding,
    assert_token_workspace_permission,
    require_workspace_for_name,
)


def test_assert_read_allows_read_grant(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="read")],
    )
    assert_token_workspace_permission(ctx, workspace.id, "read")


def test_assert_read_allows_write_grant(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="write")],
    )
    assert_token_workspace_permission(ctx, workspace.id, "read")


def test_assert_read_denies_wrong_workspace(workspace: Workspace, workspace_b: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="read")],
    )
    with pytest.raises(HTTPException) as ei:
        assert_token_workspace_permission(ctx, workspace_b.id, "read")
    assert ei.value.status_code == status.HTTP_403_FORBIDDEN
    assert "Token not authorized for this workspace" in ei.value.detail


def test_assert_write_allows_write_only(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="write")],
    )
    assert_token_workspace_permission(ctx, workspace.id, "write")


def test_assert_write_denies_read_grant(workspace: Workspace) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[TokenWorkspaceBinding(workspace_id=workspace.id, permission="read")],
    )
    with pytest.raises(HTTPException) as ei:
        assert_token_workspace_permission(ctx, workspace.id, "write")
    assert ei.value.status_code == status.HTTP_403_FORBIDDEN
    assert "Token permission insufficient" in ei.value.detail


def test_assert_multi_workspace_binding_grants_access(
    workspace: Workspace, workspace_b: Workspace
) -> None:
    ctx = ServiceTokenContext(
        token_id=1,
        bindings=[
            TokenWorkspaceBinding(workspace_id=workspace.id, permission="write"),
            TokenWorkspaceBinding(workspace_id=workspace_b.id, permission="read"),
        ],
    )
    assert_token_workspace_permission(ctx, workspace.id, "write")
    assert_token_workspace_permission(ctx, workspace_b.id, "read")
    with pytest.raises(HTTPException):
        assert_token_workspace_permission(ctx, workspace_b.id, "write")
```

删除旧的 `ServiceTokenContext(token_id=1, workspace_id=..., permission=...)` 构造。

- [ ] **Step 3: 运行测试确认失败**

Run: `cd OpenRag && python -m pytest tests/test_service_token_deps.py tests/test_service_token_workspace.py -v`
Expected: FAIL — `ImportError: cannot import name 'TokenWorkspaceBinding'` 及属性错误

- [ ] **Step 4: 更新 service_token_service.py**

完整替换 `openrag/src/openrag/services/service_token_service.py`：

```python
"""Resolve machine credentials from the X-OpenRag-Token header."""

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from openrag.models.service_token import ServiceToken
from openrag.models.service_token_workspace import ServiceTokenWorkspace
from openrag.models.workspace import Workspace

_READ = "read"
_WRITE = "write"


@dataclass(frozen=True)
class TokenWorkspaceBinding:
    workspace_id: int
    permission: str


@dataclass(frozen=True)
class ServiceTokenContext:
    token_id: int
    bindings: list[TokenWorkspaceBinding]


def resolve_service_token_context(db: Session, raw_token: str | None) -> ServiceTokenContext:
    if raw_token is None or not raw_token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service token",
        )
    token_value = raw_token.strip()
    if not token_value.startswith("sk-"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service token",
        )

    row = (
        db.query(ServiceToken)
        .filter(
            ServiceToken.secret == token_value,
            ServiceToken.revoked_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service token",
        )

    bindings = db.query(ServiceTokenWorkspace).filter_by(token_id=row.id).all()
    if not bindings:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token has no workspace bindings",
        )

    return ServiceTokenContext(
        token_id=row.id,
        bindings=[
            TokenWorkspaceBinding(
                workspace_id=b.workspace_id,
                permission=b.permission if b.permission in (_READ, _WRITE) else _READ,
            )
            for b in bindings
        ],
    )


def require_workspace_for_name(db: Session, workspace_name: str) -> Workspace:
    key = (workspace_name or "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )
    ws = db.query(Workspace).filter(Workspace.name == key).first()
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )
    return ws


def assert_token_workspace_permission(
    ctx: ServiceTokenContext,
    workspace_id: int,
    need: Literal["read", "write"],
) -> None:
    binding = next((b for b in ctx.bindings if b.workspace_id == workspace_id), None)
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token not authorized for this workspace",
        )
    if need == "read":
        if binding.permission not in (_READ, _WRITE):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token permission insufficient",
            )
        return
    if need == "write":
        if binding.permission != _WRITE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token permission insufficient",
            )
        return
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd OpenRag && python -m pytest tests/test_service_token_deps.py tests/test_service_token_workspace.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add openrag/src/openrag/services/service_token_service.py openrag/tests/test_service_token_deps.py openrag/tests/test_service_token_workspace.py
git commit -m "feat(service): update ServiceTokenContext to binding list model"
```

---

### Task 3: 移除 ServiceToken 的 workspace_id 和 permission 字段

此任务必须在 Task 1（关联表模型已创建）和 Task 2（服务层已切换到绑定列表）完成后执行。

**Files:**
- Modify: `openrag/src/openrag/models/service_token.py`
- Modify: `openrag/tests/test_service_token_models.py`

- [ ] **Step 1: 更新 test_service_token_models.py 删除旧字段测试**

在 `test_service_token_models.py` 中：
- 删除 `test_service_token_workspace_relationship`（使用 `token.workspace` 和 `workspace.service_tokens`）
- 修改其他测试：创建 `ServiceToken` 不再需要 `workspace_id` 和 `permission`，改为使用 `ServiceTokenWorkspace` 绑定

```python
def test_service_token_created_by_relationship(
    db_session: Session, owner: User
) -> None:
    token = ServiceToken(
        secret="opaque-secret-value-xxxxxxxxxxxxxxxx",
        name="CI token",
        created_by_user_id=owner.id,
    )
    db_session.add(token)
    db_session.commit()
    db_session.refresh(token)

    assert token in owner.created_service_tokens


def test_service_token_secret_unique(db_session: Session, owner: User) -> None:
    db_session.add_all(
        [
            ServiceToken(secret="same-secret", created_by_user_id=owner.id),
            ServiceToken(secret="same-secret", created_by_user_id=owner.id),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
```

- [ ] **Step 2: 更新 ServiceToken ORM 模型**

修改 `openrag/src/openrag/models/service_token.py` — 移除 `workspace_id`、`permission`、`workspace` relationship 和相关 index：

```python
"""Service token models for machine-to-machine API access"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from openrag.models.base import Base, TimestampMixin
from openrag.utils.pg_text import strip_pg_nul_bytes

if TYPE_CHECKING:
    from openrag.models.user import User


class ServiceToken(Base, TimestampMixin):
    """长期凭证；可授权访问多个工作区，绑定关系由 ServiceTokenWorkspace 管理。"""

    __tablename__ = "service_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    secret: Mapped[str] = mapped_column(
        String(256),
        unique=True,
        nullable=False,
        index=True,
        comment="Full service secret string (plaintext at rest per product spec)",
    )
    name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Optional human-readable label",
    )
    created_by_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="User who created the token",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the token was revoked (UTC)",
    )

    @validates("secret", "name")
    def _strip_nul_strings(self, _key: str, value: object) -> object:
        return strip_pg_nul_bytes(value)

    created_by: Mapped["User"] = relationship(
        "User",
        back_populates="created_service_tokens",
        foreign_keys=[created_by_user_id],
    )

    __table_args__ = (
        Index("idx_service_tokens_created_by", "created_by_user_id"),
    )

    def __repr__(self) -> str:
        return f"<ServiceToken(id={self.id}, name={self.name!r})>"
```

需要保留 `Index` import：

```python
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
```

- [ ] **Step 3: 更新 Workspace 模型中的反向引用**

检查 `openrag/src/openrag/models/workspace.py`，移除 `service_tokens` relationship（因为 ServiceToken 不再直接关联 workspace）。如果存在 `back_populates="service_tokens"` 或类似引用，将其移除。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd OpenRag && python -m pytest tests/test_service_token_models.py tests/test_service_token_deps.py tests/test_service_token_workspace.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/models/service_token.py openrag/src/openrag/models/workspace.py openrag/tests/test_service_token_models.py
git commit -m "feat(models): remove workspace_id and permission from ServiceToken"
```

---

### Task 4: 重写管理 API 路由

**Files:**
- Modify: `openrag/src/openrag/api/service_tokens_admin.py`
- Modify: `openrag/src/openrag/api/deps.py`
- Test: `openrag/tests/test_service_tokens_admin.py`

- [ ] **Step 1: 写新管理 API 的失败测试**

重写 `test_service_tokens_admin.py`，测试新的 API 路径：

```python
"""Tests for JWT service token admin routes (multi-workspace binding model)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import openrag.models  # noqa: F401
from openrag.api.deps import get_db
from openrag.api.main import app
from openrag.models import Base, ServiceTokenWorkspace, User, Workspace, WorkspaceMember
from openrag.security import create_access_token, hash_password

TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db() -> Session:
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db: Session):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    if prev is not None:
        app.dependency_overrides[get_db] = prev
    else:
        app.dependency_overrides.pop(get_db, None)


def _auth(user_id: int) -> dict[str, str]:
    token = create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def owner(db: Session) -> User:
    u = User(
        username="st_owner",
        email="st_owner@example.com",
        password_hash=hash_password("pw"),
        full_name="Owner",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def reader(db: Session) -> User:
    u = User(
        username="st_reader",
        email="st_reader@example.com",
        password_hash=hash_password("pw"),
        full_name="Reader",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def other(db: Session) -> User:
    u = User(
        username="st_other",
        email="st_other@example.com",
        password_hash=hash_password("pw"),
        full_name="Other",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def workspace_a(db: Session, owner: User) -> Workspace:
    ws = Workspace(name="WSA", slug="wsa", owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role="write"))
    db.commit()
    return ws


@pytest.fixture
def workspace_b(db: Session, owner: User) -> Workspace:
    ws = Workspace(name="WSB", slug="wsb", owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role="write"))
    db.commit()
    return ws


# --- CREATE ---

def test_create_token_with_single_workspace(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "name": "single-ws",
            "workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}],
        },
        headers=_auth(owner.id),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["secret"].startswith("sk-")
    assert len(data["workspaces"]) == 1
    assert data["workspaces"][0]["workspace_id"] == workspace_a.id
    assert data["workspaces"][0]["permission"] == "read"


def test_create_token_with_multiple_workspaces(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "name": "multi-ws",
            "workspaces": [
                {"workspace_id": workspace_a.id, "permission": "write"},
                {"workspace_id": workspace_b.id, "permission": "read"},
            ],
        },
        headers=_auth(owner.id),
    )
    assert r.status_code == 201
    data = r.json()
    assert len(data["workspaces"]) == 2


def test_create_token_empty_workspaces_array_returns_400(
    client: TestClient, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"name": "empty", "workspaces": []},
        headers=_auth(owner.id),
    )
    assert r.status_code == 400


def test_create_token_duplicate_workspace_id_returns_400(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "workspaces": [
                {"workspace_id": workspace_a.id, "permission": "read"},
                {"workspace_id": workspace_a.id, "permission": "write"},
            ],
        },
        headers=_auth(owner.id),
    )
    assert r.status_code == 400


def test_create_token_user_lacks_workspace_permission_returns_403(
    client: TestClient, db: Session, workspace_a: Workspace, reader: User
) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_a.id, user_id=reader.id, role="read"))
    db.commit()

    r = client.post(
        "/service-tokens",
        json={
            "workspaces": [{"workspace_id": workspace_a.id, "permission": "write"}],
        },
        headers=_auth(reader.id),
    )
    assert r.status_code == 403


def test_create_unauthorized(client: TestClient) -> None:
    r = client.post("/service-tokens", json={"workspaces": [{"workspace_id": 1, "permission": "read"}]})
    assert r.status_code == 401


# --- LIST ---

def test_list_all_tokens(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )

    r = client.get("/service-tokens", headers=_auth(owner.id))
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_list_tokens_filtered_by_workspace(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r1 = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    r2 = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_b.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    tid_a = r1.json()["id"]
    tid_b = r2.json()["id"]

    r_filt = client.get(
        "/service-tokens",
        params={"workspace_id": workspace_a.id},
        headers=_auth(owner.id),
    )
    assert r_filt.status_code == 200
    ids = [x["id"] for x in r_filt.json()]
    assert tid_a in ids
    assert tid_b not in ids


# --- PATCH BINDINGS ---

def test_patch_add_binding(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"add": [{"workspace_id": workspace_b.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 200
    assert len(r_patch.json()["workspaces"]) == 2


def test_patch_update_binding_permission(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"update": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 200
    ws_map = {w["workspace_id"]: w["permission"] for w in r_patch.json()["workspaces"]}
    assert ws_map[workspace_a.id] == "write"


def test_patch_remove_binding(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "workspaces": [
                {"workspace_id": workspace_a.id, "permission": "read"},
                {"workspace_id": workspace_b.id, "permission": "write"},
            ],
        },
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"remove": [{"workspace_id": workspace_a.id}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 200
    assert len(r_patch.json()["workspaces"]) == 1


def test_patch_add_duplicate_binding_returns_409(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"add": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 409


def test_patch_revoked_token_returns_400(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]
    client.delete(f"/service-tokens/{tid}", headers=_auth(owner.id))

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"add": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 400


def test_patch_forbidden_for_non_creator(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User, other: User
) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_a.id, user_id=other.id, role="write"))
    db.commit()

    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"update": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(other.id),
    )
    assert r_patch.status_code == 403


# --- REVOKE & SECRET (unchanged) ---

def test_revoke_and_secret_flow(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]
    secret = r.json()["secret"]

    r_sec = client.get(f"/service-tokens/{tid}/secret", headers=_auth(owner.id))
    assert r_sec.status_code == 200
    assert r_sec.json()["secret"] == secret

    r_del = client.delete(f"/service-tokens/{tid}", headers=_auth(owner.id))
    assert r_del.status_code == 200

    r_list = client.get("/service-tokens", headers=_auth(owner.id))
    row = next(x for x in r_list.json() if x["id"] == tid)
    assert row["revoked_at"] is not None


def test_system_admin_can_reveal_secret(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User, other: User
) -> None:
    other.is_admin = True
    db.add(other)
    db.commit()

    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_sec = client.get(f"/service-tokens/{tid}/secret", headers=_auth(other.id))
    assert r_sec.status_code == 200


def test_other_user_cannot_reveal_secret(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User, other: User
) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_a.id, user_id=other.id, role="write"))
    db.commit()

    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_sec = client.get(f"/service-tokens/{tid}/secret", headers=_auth(other.id))
    assert r_sec.status_code == 403
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd OpenRag && python -m pytest tests/test_service_tokens_admin.py -v`
Expected: FAIL — 404 on `/service-tokens` (POST) route not found

- [ ] **Step 3: 重写 service_tokens_admin.py**

完整替换 `openrag/src/openrag/api/service_tokens_admin.py`：

```python
"""JWT-only admin API for machine service tokens (multi-workspace binding model)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.models.service_token import ServiceToken
from openrag.models.service_token_workspace import ServiceTokenWorkspace
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User

router = APIRouter(tags=["service-tokens"])


def _generate_secret() -> str:
    return "sk-" + secrets.token_urlsafe(32)


def _secret_preview(secret: str) -> str:
    if len(secret) < 8:
        return "sk-****"
    return "sk-****" + secret[-4:]


def _assert_can_manage_token(user: User, token: ServiceToken) -> None:
    if user.is_admin or token.created_by_user_id == user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not allowed to manage this service token",
    )


def _assert_user_has_write_or_admin_on_workspace(user: User, workspace_id: int, db: Session) -> None:
    if user.is_admin:
        return
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user.id,
    ).first()
    if member and member.role == "write":
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to authorize access to this workspace",
    )


class WorkspaceBindingRequest(BaseModel):
    workspace_id: int
    permission: str = Field(pattern=r"^(read|write)$")


class ServiceTokenCreateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    workspaces: List[WorkspaceBindingRequest] = Field(..., min_length=1)


class BindingUpdateItem(BaseModel):
    workspace_id: int
    permission: str = Field(pattern=r"^(read|write)$")


class BindingRemoveItem(BaseModel):
    workspace_id: int


class ServiceTokenPatchBindingsRequest(BaseModel):
    add: Optional[List[BindingUpdateItem]] = None
    update: Optional[List[BindingUpdateItem]] = None
    remove: Optional[List[BindingRemoveItem]] = None


class WorkspaceBindingResponse(BaseModel):
    workspace_id: int
    workspace_name: str
    permission: str


class ServiceTokenCreatedResponse(BaseModel):
    id: int
    secret: str
    name: Optional[str]
    created_at: str
    workspaces: List[WorkspaceBindingResponse]


class ServiceTokenListItem(BaseModel):
    id: int
    name: Optional[str]
    secret_preview: str
    revoked_at: Optional[str]
    created_by_user_id: int
    workspaces: List[WorkspaceBindingResponse]


class ServiceTokenSecretResponse(BaseModel):
    secret: str


class MessageResponse(BaseModel):
    message: str


def _build_binding_response(binding: ServiceTokenWorkspace, db: Session) -> WorkspaceBindingResponse:
    ws = db.query(Workspace).filter(Workspace.id == binding.workspace_id).first()
    return WorkspaceBindingResponse(
        workspace_id=binding.workspace_id,
        workspace_name=ws.name if ws else f"workspace-{binding.workspace_id}",
        permission=binding.permission,
    )


def _build_token_list_item(token: ServiceToken, db: Session) -> ServiceTokenListItem:
    bindings = db.query(ServiceTokenWorkspace).filter_by(token_id=token.id).all()
    return ServiceTokenListItem(
        id=token.id,
        name=token.name,
        secret_preview=_secret_preview(token.secret),
        revoked_at=token.revoked_at.isoformat() if token.revoked_at else None,
        created_by_user_id=token.created_by_user_id,
        workspaces=[_build_binding_response(b, db) for b in bindings],
    )


@router.post(
    "/service-tokens",
    response_model=ServiceTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_service_token(
    body: ServiceTokenCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceTokenCreatedResponse:
    ws_ids = [w.workspace_id for w in body.workspaces]
    if len(ws_ids) != len(set(ws_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate workspace binding",
        )

    for w in body.workspaces:
        _assert_user_has_write_or_admin_on_workspace(current_user, w.workspace_id, db)

    secret = _generate_secret()
    row = ServiceToken(
        secret=secret,
        name=body.name,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    for w in body.workspaces:
        binding = ServiceTokenWorkspace(
            token_id=row.id,
            workspace_id=w.workspace_id,
            permission=w.permission,
        )
        db.add(binding)
    db.commit()

    bindings = db.query(ServiceTokenWorkspace).filter_by(token_id=row.id).all()
    return ServiceTokenCreatedResponse(
        id=row.id,
        secret=secret,
        name=row.name,
        created_at=row.created_at.isoformat(),
        workspaces=[_build_binding_response(b, db) for b in bindings],
    )


@router.get(
    "/service-tokens",
    response_model=List[ServiceTokenListItem],
)
async def list_service_tokens(
    workspace_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ServiceTokenListItem]:
    if current_user.is_admin:
        if workspace_id is not None:
            binding_ids = db.query(ServiceTokenWorkspace.token_id).filter_by(
                workspace_id=workspace_id
            ).subquery()
            rows = db.query(ServiceToken).filter(ServiceToken.id.in_(binding_ids)).all()
        else:
            rows = db.query(ServiceToken).all()
    else:
        user_ws_ids = [
            m.workspace_id for m in db.query(WorkspaceMember).filter_by(user_id=current_user.id).all()
        ]
        binding_ids = db.query(ServiceTokenWorkspace.token_id).filter(
            ServiceTokenWorkspace.workspace_id.in_(user_ws_ids)
        ).subquery()
        rows = db.query(ServiceToken).filter(ServiceToken.id.in_(binding_ids)).all()

    return [_build_token_list_item(t, db) for t in rows]


@router.patch(
    "/service-tokens/{token_id}/workspaces",
    response_model=ServiceTokenListItem,
)
async def patch_token_bindings(
    token_id: int,
    body: ServiceTokenPatchBindingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceTokenListItem:
    token = db.query(ServiceToken).filter(ServiceToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service token not found")
    _assert_can_manage_token(current_user, token)

    if token.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify bindings on a revoked token",
        )

    # add
    for item in (body.add or []):
        _assert_user_has_write_or_admin_on_workspace(current_user, item.workspace_id, db)
        existing = db.query(ServiceTokenWorkspace).filter_by(
            token_id=token.id, workspace_id=item.workspace_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Token already has a binding for this workspace",
            )
        db.add(ServiceTokenWorkspace(
            token_id=token.id,
            workspace_id=item.workspace_id,
            permission=item.permission,
        ))

    # update
    for item in (body.update or []):
        _assert_user_has_write_or_admin_on_workspace(current_user, item.workspace_id, db)
        existing = db.query(ServiceTokenWorkspace).filter_by(
            token_id=token.id, workspace_id=item.workspace_id
        ).first()
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Binding not found for this workspace",
            )
        existing.permission = item.permission
        db.add(existing)

    # remove
    for item in (body.remove or []):
        existing = db.query(ServiceTokenWorkspace).filter_by(
            token_id=token.id, workspace_id=item.workspace_id
        ).first()
        if existing:
            db.delete(existing)

    db.commit()
    return _build_token_list_item(token, db)


@router.delete("/service-tokens/{token_id}", response_model=MessageResponse)
async def revoke_service_token(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    token = db.query(ServiceToken).filter(ServiceToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service token not found")
    _assert_can_manage_token(current_user, token)
    if token.revoked_at is not None:
        return MessageResponse(message="Token already revoked")
    token.revoked_at = datetime.now(timezone.utc)
    db.add(token)
    db.commit()
    return MessageResponse(message="Token revoked")


@router.get(
    "/service-tokens/{token_id}/secret",
    response_model=ServiceTokenSecretResponse,
)
async def get_service_token_secret(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceTokenSecretResponse:
    token = db.query(ServiceToken).filter(ServiceToken.id == token_id).first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service token not found")
    _assert_can_manage_token(current_user, token)
    return ServiceTokenSecretResponse(secret=token.secret)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd OpenRag && python -m pytest tests/test_service_tokens_admin.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/api/service_tokens_admin.py openrag/tests/test_service_tokens_admin.py
git commit -m "feat(api): rewrite service token admin routes for multi-workspace bindings"
```

---

### Task 5: 更新 service_list_workspaces 端点

**Files:**
- Modify: `openrag/src/openrag/api/service_api.py`

- [ ] **Step 1: 更新 service_list_workspaces 端点**

在 `service_api.py` 中替换 `service_list_workspaces` 函数：

```python
@router.get("/workspaces")
async def service_list_workspaces(
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the workspace(s) accessible to the authenticated service token."""
    items = []
    for binding in ctx.bindings:
        ws = db.query(Workspace).filter(Workspace.id == binding.workspace_id).first()
        if ws is not None:
            items.append({
                "name": ws.name,
                "slug": ws.slug,
                "permission": binding.permission,
            })
    return {"workspaces": items}
```

同时更新 import，确保 `ServiceTokenContext` 来自新的服务层（已经正确导入）。

- [ ] **Step 2: 运行 service API 相关测试确认没有破坏**

Run: `cd OpenRag && python -m pytest tests/test_service_token_deps.py tests/test_service_token_workspace.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add openrag/src/openrag/api/service_api.py
git commit -m "feat(api): update service_list_workspaces to return binding list"
```

---

### Task 6: 迁移 SQL 脚本

**Files:**
- Create: `openrag/scripts/sql/2026-05-06-service-token-multi-workspace.sql`

- [ ] **Step 1: 创建迁移脚本**

创建 `openrag/scripts/sql/2026-05-06-service-token-multi-workspace.sql`：

```sql
-- Step 1: Create the junction table
CREATE TABLE IF NOT EXISTS service_token_workspaces (
    id SERIAL PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES service_tokens(id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    permission VARCHAR(16) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_stw_token_workspace UNIQUE (token_id, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_stw_token ON service_token_workspaces(token_id);
CREATE INDEX IF NOT EXISTS idx_stw_workspace ON service_token_workspaces(workspace_id);

-- Step 2: Migrate existing bindings (active tokens)
INSERT INTO service_token_workspaces (token_id, workspace_id, permission, created_at)
SELECT id, workspace_id, permission, created_at
FROM service_tokens
WHERE workspace_id IS NOT NULL AND revoked_at IS NULL;

-- Step 3: Migrate revoked tokens (preserve audit history)
INSERT INTO service_token_workspaces (token_id, workspace_id, permission, created_at)
SELECT id, workspace_id, permission, created_at
FROM service_tokens
WHERE workspace_id IS NOT NULL AND revoked_at IS NOT NULL;

-- Step 4: Drop old columns
ALTER TABLE service_tokens DROP COLUMN IF EXISTS workspace_id;
ALTER TABLE service_tokens DROP COLUMN IF EXISTS permission;

-- Step 5: Drop old index
DROP INDEX IF EXISTS idx_service_tokens_workspace;
```

- [ ] **Step 2: Commit**

```bash
git add openrag/scripts/sql/2026-05-06-service-token-multi-workspace.sql
git commit -m "feat(migration): add multi-workspace service token migration SQL"
```

---

### Task 7: 更新前端 TypeScript 类型

**Files:**
- Modify: `web/src/types/index.ts`

- [ ] **Step 1: 更新 ServiceToken 相关类型**

在 `web/src/types/index.ts` 中修改 `ServiceTokenListItem` 和 `ServiceTokenCreated`，新增绑定类型：

```typescript
export interface TokenWorkspaceBinding {
  workspace_id: number;
  workspace_name: string;
  permission: 'read' | 'write';
}

export interface ServiceTokenListItem {
  id: number;
  name: string | null;
  secret_preview: string;
  revoked_at: string | null;
  created_by_user_id: number;
  workspaces: TokenWorkspaceBinding[];
}

export interface ServiceTokenCreated {
  id: number;
  secret: string;
  name: string | null;
  created_at: string;
  workspaces: TokenWorkspaceBinding[];
}

export interface WorkspaceBindingRequest {
  workspace_id: number;
  permission: 'read' | 'write';
}

export interface BindingPatchRequest {
  add?: WorkspaceBindingRequest[];
  update?: WorkspaceBindingRequest[];
  remove?: { workspace_id: number }[];
}
```

删除旧的 `ServiceTokenListItem`（含 `permission`, `workspace_id`）和 `ServiceTokenCreated`（不含 `workspaces`）。

- [ ] **Step 2: Commit**

```bash
git add web/src/types/index.ts
git commit -m "feat(types): update ServiceToken types for multi-workspace bindings"
```

---

### Task 8: 更新前端 API 服务

**Files:**
- Modify: `web/src/services/api.ts`

- [ ] **Step 1: 重写 serviceTokensAPI**

在 `api.ts` 中替换 `serviceTokensAPI`，并更新 import：

更新 import（从 `../types` 增加新类型）：

```typescript
import type {
  ...,
  ServiceTokenListItem,
  ServiceTokenCreated,
  WorkspaceBindingRequest,
  BindingPatchRequest,
} from '../types';
```

替换 `serviceTokensAPI`：

```typescript
export const serviceTokensAPI = {
  create: async (
    body: { name?: string; workspaces: WorkspaceBindingRequest[] }
  ): Promise<ServiceTokenCreated> => {
    const response = await api.post('/service-tokens', body);
    return response.data;
  },
  list: async (
    workspaceId?: number
  ): Promise<ServiceTokenListItem[]> => {
    const params: Record<string, number> = {};
    if (workspaceId != null) params.workspace_id = workspaceId;
    const response = await api.get('/service-tokens', { params });
    return response.data;
  },
  patchBindings: async (
    tokenId: number,
    body: BindingPatchRequest
  ): Promise<ServiceTokenListItem> => {
    const response = await api.patch(`/service-tokens/${tokenId}/workspaces`, body);
    return response.data;
  },
  revoke: async (tokenId: number): Promise<{ message: string }> => {
    const response = await api.delete(`/service-tokens/${tokenId}`);
    return response.data;
  },
  getSecret: async (tokenId: number): Promise<{ secret: string }> => {
    const response = await api.get(`/service-tokens/${tokenId}/secret`);
    return response.data;
  },
};
```

- [ ] **Step 2: Commit**

```bash
git add web/src/services/api.ts
git commit -m "feat(api): update frontend serviceTokensAPI for multi-workspace"
```

---

### Task 9: 重写前端 ServiceTokens 页面

**Files:**
- Modify: `web/src/pages/ServiceTokens.tsx`

- [ ] **Step 1: 重写 ServiceTokens.tsx**

完整替换页面组件。核心变更：

1. 移除 workspace 选择器和 `selectedWorkspaceId`
2. Token 列表新增"关联 workspace"列，每个绑定显示为标签
3. 创建弹窗：多选 workspace + 每个独立权限下拉框
4. 新增"管理绑定"弹窗（add/update/remove）
5. 移除旧的"修改权限"弹窗

关键代码结构（完整文件过长，此处列出核心变更点）：

```typescript
import type { ServiceTokenListItem, TokenWorkspaceBinding, WorkspaceBindingRequest } from '../types';

// 移除 selectedWorkspaceId，改为全局加载
const [tokens, setTokens] = useState<ServiceTokenListItem[]>([]);
const [bindModalOpen, setBindModalOpen] = useState(false);
const [bindRow, setBindRow] = useState<ServiceTokenListItem | null>(null);

// 加载所有 token
const loadTokens = useCallback(async () => {
  setLoading(true);
  try {
    const data = await serviceTokensAPI.list();
    setTokens(data);
  } catch (err) {
    message.error(formatApiError(err));
  } finally {
    setLoading(false);
  }
}, []);

// 创建弹窗使用 workspaces 数组
const submitCreate = async () => {
  try {
    const v = await createForm.validateFields();
    const workspaces: WorkspaceBindingRequest[] = v.workspaces || [];
    if (workspaces.length === 0) {
      message.error(t('serviceTokens.at_least_one_binding'));
      return;
    }
    const created = await serviceTokensAPI.create({
      name: v.name?.trim() || undefined,
      workspaces,
    });
    // ... show secret modal
    void loadTokens();
  } catch (err) { ... }
};

// 表格列
const columns = [
  { title: 'ID', dataIndex: 'id', key: 'id', width: 72 },
  { title: t('serviceTokens.col_name'), dataIndex: 'name', key: 'name',
    render: (n: string | null) => n || '—' },
  { title: t('serviceTokens.col_preview'), dataIndex: 'secret_preview', key: 'secret_preview' },
  {
    title: t('serviceTokens.col_workspaces'),
    key: 'workspaces',
    render: (_: unknown, row: ServiceTokenListItem) => (
      <Space wrap>
        {row.workspaces.map((w) => (
          <Tag key={w.workspace_id} color={w.permission === 'write' ? 'blue' : 'default'}>
            {w.workspace_name}: {w.permission}
          </Tag>
        ))}
      </Space>
    ),
  },
  { title: t('serviceTokens.col_revoked'), ... },
  { title: t('serviceTokens.col_actions'), ... },
];

// 创建弹窗中的多 workspace 绑定表单
// 使用 Form.List 实现动态行
<Form.List name="workspaces" initialValue={[{ permission: 'write' }]}>
  {(fields, { add, remove }) => (
    <>
      {fields.map(({ key, name, ...restField }) => (
        <Space key={key} align="baseline">
          <Form.Item {...restField} name={[name, 'workspace_id']} rules={[{ required: true }]}>
            <Select
              style={{ width: 200 }}
              options={workspaces.map(w => ({ label: w.name, value: w.id }))}
              placeholder={t('serviceTokens.select_workspace')}
            />
          </Form.Item>
          <Form.Item {...restField} name={[name, 'permission']} rules={[{ required: true }]}>
            <Select
              style={{ width: 120 }}
              options={[
                { value: 'read', label: t('serviceTokens.perm_read') },
                { value: 'write', label: t('serviceTokens.perm_write') },
              ]}
            />
          </Form.Item>
          {fields.length > 1 && <Button onClick={() => remove(name)}>×</Button>}
        </Space>
      ))}
      <Button type="dashed" onClick={() => add()} block>
        + {t('serviceTokens.add_workspace')}
      </Button>
    </>
  )}
</Form.List>
```

注意：此任务需要实现者编写完整文件。上面的代码片段是关键变更点的指引，不是完整文件。实现者应基于现有页面结构，按照上述模式重写。

- [ ] **Step 2: Commit**

```bash
git add web/src/pages/ServiceTokens.tsx
git commit -m "feat(web): rework ServiceTokens page for multi-workspace bindings"
```

---

### Task 10: 更新 i18n 翻译

**Files:**
- Modify: `web/src/i18n/locales/en.json`
- Modify: `web/src/i18n/locales/zh.json`

- [ ] **Step 1: 更新 en.json serviceTokens 部分**

替换 `en.json` 中的 `"serviceTokens"` 部分：

```json
"serviceTokens": {
  "title": "Service tokens (integration API)",
  "hint": "Each token can be authorized for multiple workspaces with independent permissions. Manage workspace bindings per token. Masked previews in the list; full secret shown once at creation. Revoked tokens are rejected by /service/v1.",
  "create": "New token",
  "load_failed": "Failed to load service tokens",
  "created_title": "Token created",
  "created_hint": "Copy and store this secret now. It cannot be retrieved again from the create API.",
  "copy_secret": "Copy secret",
  "copied": "Copied to clipboard",
  "secret_title": "Full secret",
  "show_secret": "Show secret",
  "manage_bindings": "Manage bindings",
  "revoke": "Revoke",
  "revoke_confirm": "Revoke this token? Integrations will stop working immediately.",
  "revoked_ok": "Token revoked",
  "yes": "OK",
  "no": "Cancel",
  "create_modal_title": "Create service token",
  "field_name": "Name (optional)",
  "name_placeholder": "e.g. CI, backup job",
  "field_permission": "Permission",
  "perm_read": "Read",
  "perm_write": "Read & write",
  "add_workspace": "Add workspace",
  "at_least_one_binding": "At least one workspace binding is required",
  "select_workspace": "Select workspace",
  "col_name": "Name",
  "col_preview": "Secret preview",
  "col_workspaces": "Workspaces",
  "col_revoked": "Revoked at",
  "col_actions": "Actions",
  "binding_modal_title": "Manage workspace bindings",
  "add_binding": "Add binding",
  "update_binding": "Update permission",
  "remove_binding": "Remove",
  "no_bindings": "No workspace bindings",
  "permission_updated": "Binding updated",
  "binding_added": "Binding added",
  "binding_removed": "Binding removed"
}
```

- [ ] **Step 2: 更新 zh.json serviceTokens 部分**

```json
"serviceTokens": {
  "title": "服务令牌（集成 API）",
  "hint": "每个令牌可授权访问多个项目空间，各项目空间拥有独立权限。可在「管理绑定」中增删改项目空间授权。列表仅显示脱敏预览；完整密钥创建时展示一次。吊销后立即无法调用 /service/v1。",
  "create": "新建令牌",
  "load_failed": "加载令牌列表失败",
  "created_title": "令牌已创建",
  "created_hint": "请立即复制并安全保存以下密钥，关闭后将无法再次通过创建接口获取。",
  "copy_secret": "复制密钥",
  "copied": "已复制到剪贴板",
  "secret_title": "完整密钥",
  "show_secret": "显示密钥",
  "manage_bindings": "管理绑定",
  "revoke": "吊销",
  "revoke_confirm": "确定吊销该令牌？集成将无法继续使用。",
  "revoked_ok": "已吊销",
  "yes": "确定",
  "no": "取消",
  "create_modal_title": "新建服务令牌",
  "field_name": "名称（可选）",
  "name_placeholder": "例如 CI、备份脚本",
  "field_permission": "权限",
  "perm_read": "只读",
  "perm_write": "读写",
  "add_workspace": "添加项目空间",
  "at_least_one_binding": "至少需要一个项目空间绑定",
  "select_workspace": "选择项目空间",
  "col_name": "名称",
  "col_preview": "密钥预览",
  "col_workspaces": "项目空间",
  "col_revoked": "吊销时间",
  "col_actions": "操作",
  "binding_modal_title": "管理项目空间绑定",
  "add_binding": "添加绑定",
  "update_binding": "修改权限",
  "remove_binding": "移除",
  "no_bindings": "无项目空间绑定",
  "permission_updated": "绑定已更新",
  "binding_added": "绑定已添加",
  "binding_removed": "绑定已移除"
}
```

- [ ] **Step 3: Commit**

```bash
git add web/src/i18n/locales/en.json web/src/i18n/locales/zh.json
git commit -m "feat(i18n): update service token translations for multi-workspace"
```

---

### Task 11: 更新接入指南文档

**Files:**
- Modify: `docs/05-Service-Token接入指南.md`

- [ ] **Step 1: 更新接入指南文档以反映多 workspace API**

关键变更：
1. 创建 API：`POST /service-tokens`（请求体含 `workspaces` 数组）
2. 列表 API：`GET /service-tokens`（可选 `workspace_id` 过滤）
3. 绑定管理：`PATCH /service-tokens/{id}/workspaces`
4. 服务 API `/service/v1/workspaces` 返回绑定列表
5. 移除旧的 workspace-nested 路径说明

- [ ] **Step 2: Commit**

```bash
git add docs/05-Service-Token接入指南.md
git commit -m "docs: update service token integration guide for multi-workspace"
```

---

### Task 12: 最终集成验证

- [ ] **Step 1: 运行所有后端测试**

Run: `cd OpenRag && python -m pytest tests/test_service_token_models.py tests/test_service_token_deps.py tests/test_service_token_workspace.py tests/test_service_tokens_admin.py -v`
Expected: PASS

- [ ] **Step 2: 运行前端类型检查**

Run: `cd web && npx tsc --noEmit`
Expected: 无类型错误

- [ ] **Step 3: Commit（如有修复）**

如果 Step 1 或 Step 2 发现问题，修复后提交。