# 单文档唯一 tag — Phase 1（核心 tag + 检索）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让单文件上传可带一个 workspace 内唯一的 `tag`，并新增一个 service-token 的「按 tag 取文档元数据」只读接口。

**Architecture:** 在 `files` 表加一列 `tag` + `(workspace_id, tag)` 唯一约束；唯一写入 chokepoint `ingest_new_file()` 透传并校验 `tag`（字符集正则 + workspace 内查重前置于建目录 + 提交时按约束名区分 `IntegrityError`）；两个上传入口加可选 `tag` Form 并回显；新增 workspace-scoped `GET /service/v1/workspaces/{name}/documents/by-tag`；前端单文件上传弹窗加 tag 输入与批量守卫。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Alembic（生产 PostgreSQL、测试 SQLite in-memory）；pytest；前端 React + antd + vitest。

**Scope note:** 本计划只覆盖设计文档（`docs/superpowers/specs/2026-06-26-single-file-unique-tag-design.md`）§8.3 的**第一阶段**。**不含**软删除 `deleted_at`、`DELETE`/`upsert` 端点、检索/预览过滤——那些在 Phase 2、Phase 3 计划。因此本阶段：
- 迁移只加 `tag` 列（`deleted_at` 留给 Phase 2 的迁移 `20260626_0005`）。
- `GET by-tag` 查询**不含** `deleted_at IS NULL`（列还不存在），Phase 2 再补该过滤。

---

## 文件结构（本阶段创建/修改）

| 文件 | 职责 |
|---|---|
| `openrag/src/openrag/models/file.py`（改） | `File` 加 `tag` 列 + `@validates` + `uq_files_workspace_tag` |
| `openrag/alembic/versions/20260626_0004_add_file_tag.py`（建） | 加 `tag` 列 + 唯一约束的迁移 |
| `openrag/src/openrag/services/file_ingest.py`（改） | `ingest_new_file` 加 `tag` 参数：正则校验、workspace 内查重前置、`IntegrityError` 分类 helper |
| `openrag/src/openrag/api/files_api.py`（改） | `POST /files/upload` 加 `tag` Form；`FileResponse` / `FileUploadResponse` / `_file_to_response` 回显 tag；API 层重复 tag 409 回归 |
| `openrag/src/openrag/api/service_api.py`（改） | `POST .../documents` 加 `tag` Form；新增 `GET .../documents/by-tag`；`_upload_response_dict` / `_document_summary` 回显 tag；移除上传前预建目录，避免绕过 tag 预检 |
| `web/src/services/api.ts`（改） | `filesAPI.upload()` 加可选 `tag` |
| `web/src/types/index.ts`（改） | `File` 类型补 `tag?: string | null`，与后端列表/详情/上传响应一致 |
| `web/src/components/FileUpload.tsx`（改） | tag 输入 + 批量守卫（抽纯函数 `shouldBlockTaggedBatch`）+ 透传 + 清空 |
| `web/src/i18n/locales/{zh,en}.json`（改） | tag 相关文案 |
| `openrag/tests/test_file_tag_model.py`（建） | 模型级唯一性测试 |
| `openrag/tests/test_file_ingest_tag.py`（建） | `ingest_new_file` tag 校验/查重/分类测试 |
| `openrag/tests/test_files_api_tag.py`（建） | 内部 `/files/upload` 带 tag 测试 |
| `openrag/tests/test_service_api_tag.py`（建） | service 上传带 tag + `GET by-tag` 测试 |
| `web/src/services/api.tag.test.ts`（建） | `upload()` 透传 tag 测试 |
| `web/src/components/fileUploadGuard.test.ts`（建） | `shouldBlockTaggedBatch` 纯函数测试 |

---

## Task 1: `File` 模型加 `tag` 列 + 唯一约束

**Files:**
- Modify: `openrag/src/openrag/models/file.py`（`@validates` 列表 ~L84；`__table_args__` ~L174-178）
- Test: `openrag/tests/test_file_tag_model.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_file_tag_model.py`:

```python
"""Model-level uniqueness for File.tag: unique per (workspace_id, tag); NULLs free."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def ws(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w1 = Workspace(name="W1", slug="w1", owner_id=u.id)
    w2 = Workspace(name="W2", slug="w2", owner_id=u.id)
    db.add_all([w1, w2]); db.commit(); db.refresh(w1); db.refresh(w2)
    return u, w1, w2


def _file(u, w, *, uri, tag):
    return File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=u.id, workspace_id=w.id, tag=tag)


def test_same_workspace_same_tag_conflicts(db, ws):
    u, w1, _ = ws
    db.add(_file(u, w1, uri="/a.txt", tag="dup")); db.commit()
    db.add(_file(u, w1, uri="/b.txt", tag="dup"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_different_workspace_same_tag_ok(db, ws):
    u, w1, w2 = ws
    db.add(_file(u, w1, uri="/a.txt", tag="shared"))
    db.add(_file(u, w2, uri="/a.txt", tag="shared"))
    db.commit()  # no error
    assert db.query(File).filter(File.tag == "shared").count() == 2


def test_multiple_null_tags_ok(db, ws):
    u, w1, _ = ws
    db.add(_file(u, w1, uri="/a.txt", tag=None))
    db.add(_file(u, w1, uri="/b.txt", tag=None))
    db.commit()  # multiple NULLs allowed
    assert db.query(File).filter(File.workspace_id == w1.id).count() == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_file_tag_model.py -v`
Expected: FAIL — `TypeError: 'tag' is an invalid keyword argument for File`（模型还没有 `tag`）。

- [ ] **Step 3: 实现模型改动**

In `openrag/src/openrag/models/file.py`, add the column near the other optional string columns (e.g. right after the `document_type` column, before `workspace_id`):

```python
    tag: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="Per-workspace unique tag (single-file upload only)",
    )
```

Add `"tag"` to the `@validates(...)` decorator argument list (the block starting `@validates(`):

```python
    @validates(
        "uri",
        "name",
        "mime_type",
        "document_type",
        "tag",
        "l0_path",
        "l1_path",
        "l2_path",
        "l0_vector_id",
        "parser_type",
        "processing_status",
        "processing_error",
    )
```

Add the constraint to `__table_args__` (keep the existing entries):

```python
    __table_args__ = (
        UniqueConstraint("workspace_id", "uri", name="uq_files_workspace_uri"),
        UniqueConstraint("workspace_id", "tag", name="uq_files_workspace_tag"),
        Index("idx_file_owner_id", "owner_id"),
        Index("idx_file_parent_id", "parent_id"),
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_file_tag_model.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/models/file.py openrag/tests/test_file_tag_model.py
git commit -m "feat(model): add per-workspace unique File.tag column"
```

---

## Task 2: Alembic 迁移加 `tag` 列

**Files:**
- Create: `openrag/alembic/versions/20260626_0004_add_file_tag.py`

- [ ] **Step 1: 写迁移文件**

Create `openrag/alembic/versions/20260626_0004_add_file_tag.py`:

```python
"""Add per-workspace unique tag to files.

Revision ID: 20260626_0004
Revises: 20260602_0003
Create Date: 2026-06-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260626_0004"
down_revision: Union[str, None] = "20260602_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("files", sa.Column("tag", sa.String(length=128), nullable=True))
    op.create_unique_constraint("uq_files_workspace_tag", "files", ["workspace_id", "tag"])


def downgrade() -> None:
    op.drop_constraint("uq_files_workspace_tag", "files", type_="unique")
    op.drop_column("files", "tag")
```

- [ ] **Step 2: 验证迁移链路（离线）**

Run: `cd openrag && python -m alembic history | head -5`
Expected: 输出包含 `20260602_0003 -> 20260626_0004 (head)`，且无 "multiple heads" 报错。

- [ ] **Step 3: 验证可升降级（需 dev PostgreSQL 在线）**

Run: `cd openrag && python -m alembic upgrade head && python -m alembic downgrade -1 && python -m alembic upgrade head`
Expected: 三条命令都成功（升级建列+约束、降级删除、再升级）。若本地无 PG，跳过本步并在 PR 描述里标注「迁移待在有 DB 的环境验证」。

- [ ] **Step 4: 提交**

```bash
git add openrag/alembic/versions/20260626_0004_add_file_tag.py
git commit -m "feat(db): migration adds files.tag + uq_files_workspace_tag"
```

---

## Task 3: `ingest_new_file` 加 `tag` 参数（规范化 + 字符集 + 落库）

**Files:**
- Modify: `openrag/src/openrag/services/file_ingest.py`（imports 顶部；`ingest_new_file` 签名 ~L345；`FileModel(...)` ~L522）
- Test: `openrag/tests/test_file_ingest_tag.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_file_ingest_tag.py`:

```python
"""ingest_new_file tag: normalize + charset validate + persist."""

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace
from openrag.services import file_ingest
from openrag.services.file_ingest import ingest_new_file


class _FakeMinio:
    def put_file(self, *a, **k):
        return None


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _minio(monkeypatch):
    monkeypatch.setattr(file_ingest, "MinioStorage", _FakeMinio)


@pytest.fixture()
def wsowner(db: Session):
    u = User(username="o", email="o@e.com", password_hash="h", full_name="O", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


def _ingest(db, w, u, *, filename="f.txt", tag=None, path="/"):
    return ingest_new_file(
        db, w, u.id,
        parent_logical_path=path, upload_filename=filename,
        file_content=b"x", content_type="text/plain",
        require_parent_dir=False, tag=tag,
    )


def test_valid_tag_persisted(db, wsowner):
    w, u = wsowner
    row, _ = _ingest(db, w, u, tag="report-2024.v1")
    assert row.tag == "report-2024.v1"


def test_blank_tag_becomes_null(db, wsowner):
    w, u = wsowner
    row, _ = _ingest(db, w, u, tag="   ")
    assert row.tag is None


def test_none_tag_is_null(db, wsowner):
    w, u = wsowner
    row, _ = _ingest(db, w, u, tag=None)
    assert row.tag is None


@pytest.mark.parametrize("bad", ["has space", "slash/x", "q?x", "hash#x", "汉字", "a" * 129])
def test_bad_tag_rejected_400(db, wsowner, bad):
    w, u = wsowner
    with pytest.raises(HTTPException) as ei:
        _ingest(db, w, u, tag=bad)
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_file_ingest_tag.py -v`
Expected: FAIL — `ingest_new_file` 不接受 `tag` 关键字参数（`TypeError`）。

- [ ] **Step 3: 实现 tag 规范化 + 落库**

In `openrag/src/openrag/services/file_ingest.py`:

(a) 顶部 import 区加（与现有 import 同组）：

```python
import re
```

(b) 模块级常量（放在其它模块常量附近，如 `MAX_FILENAME_BYTES` 旁）：

```python
_TAG_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _normalize_tag(tag):
    """Trim; blank -> None; else must match the tag charset (raises 400)."""
    tag = (tag or "").strip()
    if not tag:
        return None
    if not _TAG_RE.match(tag):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid tag. Allowed: ^[A-Za-z0-9._:-]{1,128}$",
        )
    return tag
```

(c) 在 `ingest_new_file` 签名里加关键字参数（放在 `duplicate_status_code` 之后）：

```python
    duplicate_status_code: int = status.HTTP_400_BAD_REQUEST,
    tag: Optional[str] = None,
) -> Tuple[FileModel, Optional[Task]]:
```

(d) 在函数体早期（紧接 `parser_type` 校验之后、`require_parent_dir` 处理之前）规范化 tag：

```python
        normalized_tag = _normalize_tag(tag)
```

(e) 在 `FileModel(...)` 构造里加 `tag=normalized_tag`（与 `parser_type=...` 同级）：

```python
        file_record = FileModel(
            uri=file_uri,
            name=upload_filename,
            owner_id=owner_user_id,
            workspace_id=workspace.id,
            is_directory=False,
            size=file_size,
            mime_type=ct,
            document_type=normalized_document_type,
            parser_type=parser_type if parser_type != "auto" else None,
            tag=normalized_tag,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_file_ingest_tag.py -v`
Expected: PASS（valid/blank/none + 6 个 bad 参数化用例全过）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/tests/test_file_ingest_tag.py
git commit -m "feat(ingest): accept + validate + persist tag in ingest_new_file"
```

---

## Task 4: workspace 内查重（前置于建目录）+ `IntegrityError` 分类

**Files:**
- Modify: `openrag/src/openrag/services/file_ingest.py`（imports；`_normalize_tag` 旁加分类 helper；ingest 查重段 + commit 段）
- Test: `openrag/tests/test_file_ingest_tag.py`（追加）

- [ ] **Step 1: 追加失败测试**

Append to `openrag/tests/test_file_ingest_tag.py`:

```python
def test_duplicate_tag_same_workspace_409(db, wsowner):
    w, u = wsowner
    _ingest(db, w, u, filename="a.txt", tag="dup")
    with pytest.raises(HTTPException) as ei:
        _ingest(db, w, u, filename="b.txt", tag="dup")
    assert ei.value.status_code == status.HTTP_409_CONFLICT


def test_duplicate_tag_check_before_mkdir(db, wsowner):
    """Tag conflict must abort before ancestor dirs are created (no empty dirs left)."""
    w, u = wsowner
    _ingest(db, w, u, path="/", filename="a.txt", tag="dup")
    with pytest.raises(HTTPException):
        _ingest(db, w, u, path="/new/deep", filename="b.txt", tag="dup")
    # /new and /new/deep must NOT have been created by the aborted upload
    leftover = db.query(File).filter(
        File.workspace_id == w.id, File.is_directory.is_(True), File.uri.like("/new%")
    ).count()
    assert leftover == 0


def test_same_tag_other_workspace_ok(db, wsowner):
    w, u = wsowner
    other = Workspace(name="W2", slug="w2", owner_id=u.id)
    db.add(other); db.commit(); db.refresh(other)
    _ingest(db, w, u, filename="a.txt", tag="shared")
    row2, _ = _ingest(db, other, u, filename="a.txt", tag="shared")
    assert row2.tag == "shared"


def test_violated_unique_constraint_classifier():
    from openrag.services.file_ingest import _violated_unique_constraint

    class _E:
        def __init__(self, m): self.orig = m

    assert _violated_unique_constraint(_E('violates unique constraint "uq_files_workspace_tag"')) == "tag"
    assert _violated_unique_constraint(_E("UNIQUE constraint failed: files.workspace_id, files.tag")) == "tag"
    assert _violated_unique_constraint(_E('violates unique constraint "uq_files_workspace_uri"')) == "uri"
    assert _violated_unique_constraint(_E("UNIQUE constraint failed: files.workspace_id, files.uri")) == "uri"
    assert _violated_unique_constraint(_E("some unrelated error")) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_file_ingest_tag.py -k "duplicate or classifier or other_workspace" -v`
Expected: FAIL — 无查重逻辑（重复 tag 不报 409 / 留下空目录）；`_violated_unique_constraint` 不存在（ImportError）。

- [ ] **Step 3: 实现查重 + 分类**

In `openrag/src/openrag/services/file_ingest.py`:

(a) import 区加：

```python
from sqlalchemy.exc import IntegrityError
```

(b) 在 `_normalize_tag` 下面加分类 helper：

```python
def _violated_unique_constraint(exc):
    """Return 'tag' | 'uri' | None for a unique-violation IntegrityError.

    Works for PostgreSQL (constraint name in message) and SQLite (column list).
    """
    msg = str(getattr(exc, "orig", exc) or "")
    if "uq_files_workspace_tag" in msg or "files.tag" in msg:
        return "tag"
    if "uq_files_workspace_uri" in msg or "files.uri" in msg:
        return "uri"
    return None
```

(c) workspace 内查重——放在 `normalized_tag = _normalize_tag(tag)`（Task 3d）之后、`if require_parent_dir:` / `ensure_directory_path(...)` **之前**：

```python
        if normalized_tag is not None:
            tag_clash = (
                db.query(FileModel)
                .filter(
                    FileModel.workspace_id == workspace.id,
                    FileModel.tag == normalized_tag,
                )
                .first()
            )
            if tag_clash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Tag already in use",
                )
```

(d) 把 `db.commit()`（创建 file_record 处，~L534）包成按约束名分类：

```python
        db.add(file_record)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            kind = _violated_unique_constraint(exc)
            if kind == "tag":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Tag already in use",
                ) from exc
            if kind == "uri":
                raise HTTPException(
                    status_code=duplicate_status_code,
                    detail=f"File already exists at {file_uri}",
                ) from exc
            raise
        db.refresh(file_record)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_file_ingest_tag.py -v`
Expected: PASS（全部，含查重 409、建目录前中止、跨 workspace 复用、分类器）。

- [ ] **Step 5: 回归既有 ingest 测试**

Run: `cd openrag && python -m pytest tests/test_file_ingest_creates_dirs.py tests/test_upload_filename_length.py -v`
Expected: PASS（既有行为不回归）。

- [ ] **Step 6: 提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/tests/test_file_ingest_tag.py
git commit -m "feat(ingest): workspace-scoped tag dup-check before mkdir + IntegrityError classify"
```

---

## Task 5: 内部 `POST /files/upload` 加 `tag` Form + 回显

**Files:**
- Modify: `openrag/src/openrag/api/files_api.py`（`upload_file` ~L348；`FileResponse`/`FileUploadResponse` schema；`_file_to_response`/`_file_to_upload_response`）
- Test: `openrag/tests/test_files_api_tag.py`（上传回显 + API 层重复 tag 409）

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_files_api_tag.py`（复用 `test_files_api.py` 的既有 fixtures 与 `FakeMinioStorage`；若该文件未导出，照抄其 fixture 定义）:

```python
"""Internal /files/upload passes + echoes tag."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from openrag.api.main import app
from openrag.api.deps import get_current_user

# Reuse the fixtures + FakeMinioStorage + override factory from test_files_api.py
from tests.test_files_api import (  # type: ignore
    db, test_user, test_workspace, FakeMinioStorage,
    override_get_current_user_factory,
)


@pytest.fixture()
def client():
    return TestClient(app)


def test_upload_with_tag_persists_and_echoes(client, db, test_user, test_workspace, monkeypatch):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)

    try:
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/", "workspace_id": str(test_workspace.id), "tag": "doc-1"}
        r = client.post("/files/upload", files=files, data=data)

        assert r.status_code == status.HTTP_201_CREATED
        assert r.json().get("tag") == "doc-1"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_upload_duplicate_tag_returns_409(client, db, test_user, test_workspace, monkeypatch):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)

    try:
        first = client.post(
            "/files/upload",
            files={"file": ("a.txt", b"one", "text/plain")},
            data={"path": "/", "workspace_id": str(test_workspace.id), "tag": "dup"},
        )
        assert first.status_code == status.HTTP_201_CREATED

        second = client.post(
            "/files/upload",
            files={"file": ("b.txt", b"two", "text/plain")},
            data={"path": "/", "workspace_id": str(test_workspace.id), "tag": "dup"},
        )
        assert second.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)
```

> 若 `tests.test_files_api` 没有把这些 fixture 设为模块级可导入（它们用 `self`/类内方法），改为在本测试文件内**照抄** `test_files_api.py` 顶部的 `override_get_db`、`db`、`test_user`、`test_workspace`、`FakeMinioStorage`、`override_get_current_user_factory` 定义。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_files_api_tag.py -v`
Expected: FAIL — 响应 JSON 里没有 `tag`（端点未接收/回显），或重复 tag 没有从 API 层稳定透出 409。

- [ ] **Step 3: 实现端点 + 响应**

In `openrag/src/openrag/api/files_api.py`:

(a) `upload_file` 加 Form 参数（放在 `document_type` 之后、`current_user` 之前）：

```python
    tag: Optional[str] = Form(
        default=None, description="Unique tag within the workspace (single-file upload only)"
    ),
```

(b) 调 `ingest_new_file(...)` 时透传 `tag=tag`（与 `document_type=document_type` 同级）。

(c) 找到 `FileResponse`（pydantic 模型，files_api.py 内）并在基础响应模型上加可选字段，而不是只加在 `FileUploadResponse` 上：

```python
    tag: Optional[str] = None
```

(d) 找到 `_file_to_response(...)`，在返回的 `FileResponse(...)` 里加 `tag=file.tag`：

```python
        tag=file.tag,
```

这样 `/files` 列表、`GET /files/{id}` 详情、`/files/upload` 上传响应都会一致带 `tag`。`FileUploadResponse` 继承 `FileResponse` 后不需要重复定义字段；若 `_file_to_upload_response(...)` 没有通过 `base.model_dump()` 构造，则也要在返回的 `FileUploadResponse(...)` 里显式加 `tag=file_record.tag`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_files_api_tag.py -v`
Expected: PASS（`tag == "doc-1"`；重复 tag 返回 409）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/files_api.py openrag/tests/test_files_api_tag.py
git commit -m "feat(api): /files/upload accepts + echoes tag"
```

---

## Task 6: service 上传带 tag + 新增 `GET .../documents/by-tag`

**Files:**
- Modify: `openrag/src/openrag/api/service_api.py`（`service_upload_document` ~L295；`_upload_response_dict` L184；`_document_summary` L172；新增路由；删除上传前 `ensure_directory_path(...)` 预建目录）
- Test: `openrag/tests/test_service_api_tag.py`（service 上传回显、重复 tag + `create_dirs=true` 不落空目录、workspace-scoped by-tag）

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_service_api_tag.py`（复用 `test_service_api.py` 的 fixture 风格——照抄其顶部 `engine`/`_stub_app_startup`/`db`/`owner`/`workspace`/`service_token_headers`/`service_token_write_headers`/`client` 定义；下面只列新增用例）:

```python
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from openrag.models import File as DbFile, ServiceToken, ServiceTokenWorkspace, Workspace
from openrag.storage.minio_storage import MinioStorage


def _upload(client, ws_name, headers, *, filename="n.txt", tag=None, path="/", create_dirs=False):
    files = {"file": (filename, b"hello", "text/plain")}
    data = {"path": path}
    if tag is not None:
        data["tag"] = tag
    if create_dirs:
        data["create_dirs"] = "true"
    with patch.object(MinioStorage, "put_file", return_value=None):
        return client.post(
            f"/service/v1/workspaces/{ws_name}/documents",
            files=files, data=data, headers=headers,
        )


def _file_exists(db: Session, workspace_id: int, uri: str) -> bool:
    return db.query(DbFile).filter(DbFile.workspace_id == workspace_id, DbFile.uri == uri).first() is not None


def _insert_tagged_file(db: Session, ws: Workspace, owner, *, uri: str, tag: str) -> DbFile:
    f = DbFile(
        uri=uri,
        name=uri.rsplit("/", 1)[-1],
        owner_id=owner.id,
        workspace_id=ws.id,
        is_directory=False,
        size=3,
        mime_type="text/plain",
        tag=tag,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _read_headers_for_workspace(db: Session, owner, workspace: Workspace, secret: str) -> dict[str, str]:
    tok = ServiceToken(secret=secret, name=secret, created_by_user_id=owner.id)
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=workspace.id, permission="read"))
    db.commit()
    return {"X-OpenRag-Token": secret}


def test_service_upload_with_tag_echoes(client, db, workspace, owner, service_token_write_headers):
    r = _upload(client, workspace.name, service_token_write_headers, tag="svc-tag-1")
    assert r.status_code == 201
    assert r.json().get("tag") == "svc-tag-1"


def test_service_duplicate_tag_with_create_dirs_does_not_create_empty_dirs(
    client, db, workspace, owner, service_token_write_headers
):
    first = _upload(client, workspace.name, service_token_write_headers, filename="a.txt", tag="dup")
    assert first.status_code == 201

    second = _upload(
        client,
        workspace.name,
        service_token_write_headers,
        filename="b.txt",
        tag="dup",
        path="/new/deep",
        create_dirs=True,
    )
    assert second.status_code == 409
    db.expire_all()
    assert not _file_exists(db, workspace.id, "/new")
    assert not _file_exists(db, workspace.id, "/new/deep")


def test_get_by_tag_hit(client, db, workspace, owner, service_token_write_headers, service_token_headers):
    _upload(client, workspace.name, service_token_write_headers, filename="hit.txt", tag="findme")
    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "findme"}, headers=service_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tag"] == "findme"
    assert body["name"] == "hit.txt"


def test_get_by_tag_same_tag_is_workspace_scoped(client, db, workspace, owner, service_token_headers):
    other = Workspace(name="OtherWS", slug="other-ws", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    a = _insert_tagged_file(db, workspace, owner, uri="/a.txt", tag="shared")
    b = _insert_tagged_file(db, other, owner, uri="/b.txt", tag="shared")
    other_headers = _read_headers_for_workspace(db, owner, other, "sk-other-read")

    r1 = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "shared"}, headers=service_token_headers,
    )
    assert r1.status_code == 200
    assert r1.json()["id"] == a.id
    assert r1.json()["path"] == "/a.txt"

    r2 = client.get(
        f"/service/v1/workspaces/{other.name}/documents/by-tag",
        params={"tag": "shared"}, headers=other_headers,
    )
    assert r2.status_code == 200
    assert r2.json()["id"] == b.id
    assert r2.json()["path"] == "/b.txt"


def test_get_by_tag_unknown_404(client, db, workspace, service_token_headers):
    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "nope"}, headers=service_token_headers,
    )
    assert r.status_code == 404


def test_get_by_tag_cross_workspace_403(client, db, owner, workspace, service_token_headers):
    other = Workspace(name="OtherWS", slug="other-ws", owner_id=owner.id)
    db.add(other)
    db.commit()
    r = client.get(
        f"/service/v1/workspaces/{other.name}/documents/by-tag",
        params={"tag": "x"}, headers=service_token_headers,
    )
    assert r.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_service_api_tag.py -v`
Expected: FAIL — 上传响应无 `tag`；`/documents/by-tag` 路由不存在（404 for all，包括本应 200 的命中用例）；或 `create_dirs=true` 在重复 tag 409 前已经创建 `/new`、`/new/deep` 空目录。

- [ ] **Step 3: 实现 service 上传 tag + by-tag 路由**

In `openrag/src/openrag/api/service_api.py`:

(a) `service_upload_document` 加 Form 参数（放在 `create_dirs` 之后、`ctx` 之前）：

```python
    tag: Optional[str] = Form(default=None, description="Unique tag within the workspace"),
```

并在调 `ingest_new_file(...)` 时加 `tag=tag`。

(a-1) 删除 `service_upload_document` 中调用 `ingest_new_file(...)` 前的预建目录逻辑：

```python
    if create_dirs:
        ensure_directory_path(db, ws, path)
```

不要在 service 层提前创建目录。保留/调整为让 `ingest_new_file(..., require_parent_dir=not create_dirs, tag=tag)` 统一处理：`ingest_new_file()` 内部先规范化与查重 `tag`，再按 `require_parent_dir=False` 创建父目录。这样重复 tag 会在任何目录副作用之前返回 409，不会留下 `/new`、`/new/deep` 这类空目录；成功的 `create_dirs=true` 行为仍由既有 `test_service_upload_create_dirs_materialises_parents` 覆盖。

> 同时删除 `service_upload_document` 中那段已过时的注释（原 `# When create_dirs is set we materialise the parent path first, ...`）——它描述的是被移除的 service 层预建目录逻辑，留着会误导后续读者。

(b) `_upload_response_dict(...)` 返回的 dict 加 `"tag": file_record.tag`。

(c) `_document_summary(f)` 返回的 dict 加 `"tag": f.tag`。

(d) 新增路由（放在 `service_document_by_path` 附近）：

```python
@router.get("/workspaces/{workspace_name}/documents/by-tag")
async def service_document_by_tag(
    workspace_name: str,
    tag: str = Query(..., min_length=1, description="Exact tag within this workspace"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    f = (
        db.query(DbFile)
        .filter(
            DbFile.workspace_id == ws.id,
            DbFile.tag == tag,
            DbFile.is_directory.is_(False),
        )
        .first()
    )
    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No document with this tag")
    return _document_summary(f)
```

> 本阶段查询**不含** `deleted_at IS NULL`（列在 Phase 2 才加）。Phase 2 须给本查询补该过滤。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_service_api_tag.py -v`
Expected: PASS（上传回显 tag；重复 tag + `create_dirs=true` 不落空目录；by-tag 命中 200、未知 404、同 tag 跨 workspace 各查各的、跨 workspace 403）。

- [ ] **Step 5: 回归既有 service 测试**

Run: `cd openrag && python -m pytest tests/test_service_api.py -v`
Expected: PASS（既有 service 路由不回归）。

- [ ] **Step 6: 提交**

```bash
git add openrag/src/openrag/api/service_api.py openrag/tests/test_service_api_tag.py
git commit -m "feat(service): upload tag echo + GET workspaces/{name}/documents/by-tag"
```

---

## Task 7: 前端 — `api.ts` 透传 tag + `FileUpload` tag 输入与批量守卫

**Files:**
- Modify: `web/src/services/api.ts`（`filesAPI.upload` ~L103）
- Modify: `web/src/types/index.ts`（`File` 接口补 `tag?: string | null`）
- Modify: `web/src/components/FileUpload.tsx`
- Modify: `web/src/i18n/locales/zh.json`、`web/src/i18n/locales/en.json`
- Create: `web/src/components/fileUploadGuard.ts`（抽出纯函数守卫，便于测试）
- Test: `web/src/services/api.tag.test.ts`、`web/src/components/fileUploadGuard.test.ts`

- [ ] **Step 1: 写失败测试（守卫纯函数）**

Create `web/src/components/fileUploadGuard.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { shouldBlockTaggedBatch } from './fileUploadGuard';

describe('shouldBlockTaggedBatch', () => {
  it('blocks when tag set and a directory is dropped', () => {
    expect(shouldBlockTaggedBatch('t', true, 1)).toBe(true);
  });
  it('blocks when tag set and multiple loose files dropped', () => {
    expect(shouldBlockTaggedBatch('t', false, 3)).toBe(true);
  });
  it('allows single file with a tag', () => {
    expect(shouldBlockTaggedBatch('t', false, 1)).toBe(false);
  });
  it('allows folder/batch when tag is empty', () => {
    expect(shouldBlockTaggedBatch('', true, 5)).toBe(false);
    expect(shouldBlockTaggedBatch('   ', false, 4)).toBe(false);
  });
});
```

- [ ] **Step 2: 写失败测试（api.ts 透传 tag）**

Create `web/src/services/api.tag.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  inst: { get: vi.fn(), post: vi.fn().mockResolvedValue({ data: {} }), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
}));
vi.mock('axios', () => ({ default: { create: vi.fn(() => mocks.inst) } }));

describe('filesAPI.upload tag', () => {
  beforeEach(() => { vi.resetModules(); vi.clearAllMocks(); mocks.inst.post.mockResolvedValue({ data: {} }); });

  it('appends tag to FormData when provided', async () => {
    const { filesAPI } = await import('./api');
    const file = new File([new Blob(['x'])], 'a.txt', { type: 'text/plain' });
    await filesAPI.upload(file, 'auto', 1, '/', 'general', 'my-tag');
    const fd = mocks.inst.post.mock.calls[0][1] as FormData;
    expect(fd.get('tag')).toBe('my-tag');
  });

  it('omits tag when not provided', async () => {
    const { filesAPI } = await import('./api');
    const file = new File([new Blob(['x'])], 'a.txt', { type: 'text/plain' });
    await filesAPI.upload(file, 'auto', 1, '/', 'general');
    const fd = mocks.inst.post.mock.calls[0][1] as FormData;
    expect(fd.get('tag')).toBeNull();
  });
});
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd web && npx vitest run src/components/fileUploadGuard.test.ts src/services/api.tag.test.ts`
Expected: FAIL — `./fileUploadGuard` 不存在；`upload()` 第 6 个参数 `tag` 未实现。

- [ ] **Step 4: 实现守卫纯函数**

Create `web/src/components/fileUploadGuard.ts`:

```ts
/** When a tag is entered, only a single file may be uploaded.
 * Returns true if this drop must be blocked (tag present AND folder-or-multi-file). */
export function shouldBlockTaggedBatch(tag: string, hasDirectory: boolean, looseFileCount: number): boolean {
  if (!tag.trim()) return false;
  return hasDirectory || looseFileCount > 1;
}
```

- [ ] **Step 5: 实现 `api.ts` 透传 tag**

In `web/src/services/api.ts`, extend `filesAPI.upload`:

```ts
  upload: async (
    file: globalThis.File,
    parserType: string = 'auto',
    workspaceId: number = 1,
    path: string = '/',
    documentType: DocumentType = 'general',
    tag?: string
  ): Promise<File> => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('parser_type', parserType);
    formData.append('workspace_id', workspaceId.toString());
    formData.append('path', path);
    formData.append('document_type', documentType);
    if (tag && tag.trim()) formData.append('tag', tag.trim());
    const response = await api.post('/files/upload', formData);
    return response.data;
  },
```

- [ ] **Step 5.5: 补前端响应类型**

In `web/src/types/index.ts`, 给 `File` 接口加可选字段（与后端 `FileResponse` / `FileUploadResponse` 对齐）：

```ts
  tag?: string | null;
```

这不是 UI 功能扩展，而是避免前端拿到列表/详情/上传响应中的 `tag` 后仍被类型系统视为不存在。`npx tsc --noEmit` 必须覆盖这个变化。

- [ ] **Step 6: 跑两个前端测试确认通过**

Run: `cd web && npx vitest run src/components/fileUploadGuard.test.ts src/services/api.tag.test.ts`
Expected: PASS（守卫 4 用例 + api 2 用例）。

- [ ] **Step 7: 接线 `FileUpload.tsx`（tag 输入 + 守卫 + 透传 + 清空）**

In `web/src/components/FileUpload.tsx`:

(a) import 守卫与 i18n（i18n 已在用）：

```tsx
import { shouldBlockTaggedBatch } from './fileUploadGuard';
```

(b) 加状态与 ref（与 `parserType` 等并列）：

```tsx
  const [tag, setTag] = useState<string>('');
  const tagRef = useRef('');
  tagRef.current = tag;
```

(c) 单文件 `customRequest` 调用 `filesAPI.upload(...)` 时把 `tag` 作为第 6 参传入：

```tsx
        await filesAPI.upload(file as globalThis.File, parserType, workspaceId, uploadPath, documentType, tag);
```

成功分支里 reset 时加 `setTag('')`。

(d) capture 阶段 `onDrop`（现有 `const onDrop = (e: DragEvent) => { ... }`）开头加守卫：先算散文件数与是否含目录，若 `shouldBlockTaggedBatch(tagRef.current, hasDirectory, looseCount)` 为真，则拦截：

```tsx
      const files = Array.from(e.dataTransfer?.files ?? []);
      const looseCount = files.length;
      // entries/hasDirectory 已在现有逻辑里计算；若尚未，复用现有 entries
      if (shouldBlockTaggedBatch(tagRef.current, hasDirectory, looseCount)) {
        e.preventDefault();
        e.stopPropagation();
        message.warning(t('files.upload.tag_batch_blocked'));
        return;
      }
```

（放在现有 `if (!hasDirectory) return;` 之前，使「tag 非空 + 多散文件」也被拦截。）

(e) 在表单里加 tag 输入（放在 document_type `Form.Item` 之后）：

```tsx
          <Form.Item label={t('files.upload.tag_label')} tooltip="^[A-Za-z0-9._:-]{1,128}$">
            <Input value={tag} onChange={(e) => setTag(e.target.value)} disabled={uploading}
                   placeholder="report-2024" allowClear />
          </Form.Item>
```

(f) `customRequest` 的 catch 分支：识别 409 时提示 `t('files.upload.tag_conflict')`（与现有错误处理并列）。

- [ ] **Step 8: 加 i18n 文案**

In `web/src/i18n/locales/zh.json`（`files.upload` 节点下）：

```json
"tag_label": "唯一标签（可选）",
"tag_conflict": "该标签已被占用，请换一个",
"tag_batch_blocked": "已填写唯一标签，仅支持单文件上传；如需上传文件夹/多文件，请先清空标签"
```

In `web/src/i18n/locales/en.json`（同节点）：

```json
"tag_label": "Unique tag (optional)",
"tag_conflict": "This tag is already in use, please choose another",
"tag_batch_blocked": "A unique tag is set; only single-file upload is allowed. Clear the tag to upload a folder/multiple files."
```

- [ ] **Step 9: 跑前端测试 + 类型检查**

Run: `cd web && npx vitest run src/components/fileUploadGuard.test.ts src/services/api.tag.test.ts && npx tsc --noEmit`
Expected: PASS + 无类型错误。

- [ ] **Step 10: 提交**

```bash
git add web/src/services/api.ts web/src/types/index.ts web/src/components/FileUpload.tsx web/src/components/fileUploadGuard.ts web/src/i18n/locales/zh.json web/src/i18n/locales/en.json web/src/services/api.tag.test.ts web/src/components/fileUploadGuard.test.ts
git commit -m "feat(web): single-file tag input + batch guard + upload passthrough"
```

---

## Task 8: 阶段收尾 — 全量回归

- [ ] **Step 1: 后端全量**

Run: `cd openrag && python -m pytest tests/test_file_tag_model.py tests/test_file_ingest_tag.py tests/test_files_api_tag.py tests/test_service_api_tag.py tests/test_file_ingest_creates_dirs.py tests/test_files_api.py tests/test_service_api.py -v`
Expected: 全 PASS。

必须确认以下来自 Codex 复审补充的回归均已包含在上述测试集中：
- 内部 `/files/upload`：同 workspace 重复 tag 返回 409。
- service 上传：同 workspace 重复 tag 且 `create_dirs=true`、目标为 `/new/deep` 时返回 409，并且数据库里没有 `/new`、`/new/deep` 空目录。
- service 上传：既有 `test_service_upload_create_dirs_materialises_parents` 仍通过，证明删除 service 层预建目录后，成功路径仍能创建父目录。
- `GET .../documents/by-tag`：同一个 tag 可存在于不同 workspace；查询 workspace A 返回 A 的文件，查询 workspace B 返回 B 的文件；用 A 的 token 查 B 仍返回 403。

- [ ] **Step 2: 前端全量**

Run: `cd web && npx vitest run && npx tsc --noEmit`
Expected: 全 PASS + 无类型错误。

必须确认 `web/src/types/index.ts` 的 `File` 接口包含 `tag?: string | null`，并由 `npx tsc --noEmit` 覆盖。

- [ ] **Step 3: 标记阶段完成**

Phase 1 完成。Phase 2（软删除 `deleted_at` 子系统）、Phase 3（upsert）各自独立计划。

---

## Self-Review（计划自查）

- **Spec 覆盖**（§8.3 Phase 1）：tag 列+约束（T1/T2）、字符集校验（T3）、workspace 内查重前置（T4）、`IntegrityError` 按约束名（T4）、两个 POST 透传+回显（T5/T6）、`GET by-tag`（T6）、前端 tag 输入+显式多文件守卫（T7）。普通重复 409（T4/T6）。✅ 覆盖。**有意延后到 Phase 2/3**：`deleted_at` 及所有读/写/预览/检索过滤、`DELETE by-path`、`upsert`、同行 move、前缀删除、worker 改动——本计划顶部已显式标注。
- **占位符扫描**：无 TBD/TODO；每个实现步骤含完整代码，每个测试步骤含完整测试代码与可跑命令；已移除未写完的 `test_get_by_tag_no_read_permission_403` 片段，避免实现者复制无断言用例。
- **类型/签名一致性**：`ingest_new_file(..., tag=...)` 在 T3 定义、T5/T6 调用一致；`_violated_unique_constraint` 在 T4 定义并测；`_normalize_tag` T3 定义、T4 复用；后端 `FileResponse` 基础响应模型带 `tag`，`FileUploadResponse` 继承后自然回显；前端 `File` 接口补 `tag?: string | null`；前端 `upload(file, parser, ws, path, docType, tag?)` 第 6 参在 T5(后端 Form)/T7(前端) 一致；`shouldBlockTaggedBatch(tag, hasDirectory, looseCount)` T7 定义与 onDrop 调用一致。
- **Codex 复审补充风险已落入计划**：T5 补 API 层重复 tag 409；T6 明确删除 service 上传前 `ensure_directory_path(...)` 预建目录，改由 `ingest_new_file(..., require_parent_dir=not create_dirs, tag=tag)` 在 tag 预检后统一建目录；T6/T8 补 `create_dirs=true` 重复 tag 不落空目录、同 tag 跨 workspace by-tag 隔离、跨 workspace token 403 回归；T7/T8 补前端 `File.tag` 类型检查。
- **已知实现注意**：内部 `/files/upload` 测试依赖 `test_files_api.py` 的 fixtures——若不可跨文件导入则照抄（T5 步骤已注明）。`GET by-tag` 查询本阶段不含 `deleted_at` 过滤（列未建），Phase 2 必补。

---

## 提交记录（Phase 1 实现）

本阶段按 TDD 逐任务实现，共产生 **7 次实现提交**（Task 1–7；Task 8 为全量回归，无代码改动故不单独提交）。按时间顺序：

| # | 提交 | Task | 总体性描述 |
|---|---|---|---|
| 1 | `cd63bfe` | T1 | **模型层落地唯一 tag。** `File` 模型新增可空 `tag` 列（`String(128)`）、`(workspace_id, tag)` 唯一约束 `uq_files_workspace_tag`，并把 `tag` 纳入 `@validates` 的 NUL 字节清洗；附模型级测试证明同 workspace 同 tag 冲突、跨 workspace 同 tag 可共存、多个 NULL 不冲突。 |
| 2 | `a946375` | T2 | **数据库迁移。** 新增 Alembic 迁移 `20260626_0004`（`20260602_0003 → 0004`），`upgrade` 加 `tag` 列 + 唯一约束、`downgrade` 反向回滚；离线验证迁移链单一 head、无分叉。 |
| 3 | `62dab38` | T3 | **写入 chokepoint 接收并校验 tag。** `ingest_new_file()` 新增 `tag` 关键字参数，加入 `_normalize_tag()`（去空白→空转 None、字符集正则 `^[A-Za-z0-9._:-]{1,128}$` 校验，非法 400）并把规范化结果落库；覆盖合法/空白/None/6 类非法输入。 |
| 4 | `bc52224` | T4 | **workspace 内查重前置 + 约束冲突分类。** 在建目录（`ensure_directory_path`）之前做 workspace 内 tag 查重（命中 409，避免冲突时残留空目录），并把创建提交包成 `IntegrityError` 处理：用 `_violated_unique_constraint()` 按约束名/列名区分 tag（409）与 uri（沿用调用方状态码），兼容 PostgreSQL 与 SQLite 报错文本。 |
| 5 | `314c1d9` | T5 | **内部 JWT 上传透传 + 一致回显。** `POST /files/upload` 加 `tag` Form 并透传 ingest；`tag` 字段加在基类 `FileResponse` 上（`FileUploadResponse` 继承、`_file_to_response` 填充），使 `/files` 列表、`GET /files/{id}` 详情、上传响应一致带 `tag`；补 API 层重复 tag 409 回归。 |
| 6 | `2fc4d4f` | T6 | **外部 service-token 上传 + 按 tag 检索。** `POST .../documents` 加 `tag` Form、`_upload_response_dict`/`_document_summary` 回显 tag；新增只读 `GET /service/v1/workspaces/{name}/documents/by-tag`（workspace-scoped、404/403 语义）。同时移除 service 层上传前的 `ensure_directory_path` 预建目录，改由 `ingest_new_file(require_parent_dir=not create_dirs)` 在 tag 预检之后统一建目录——使 `create_dirs=true` 撞 tag 时 409 先于任何目录副作用、不落空目录。 |
| 7 | `3f7111b` | T7 | **前端单文件 tag 输入与批量守卫。** `filesAPI.upload()` 加可选第 6 参 `tag` 透传；`File` 类型补 `tag?: string \| null`；抽纯函数 `shouldBlockTaggedBatch()` 并在 `FileUpload` 的 tag 输入框、拖拽 `onDrop` 守卫（填了 tag 时拖目录/多文件即拦截）、成功后清空、409→tag 冲突文案中接线；附守卫与透传单测、中英文案。 |

**验证（Task 8 全量回归）**：后端 tag 套件 + `ingest_creates_dirs` + `service_api` 既有用例 66 passed；`test_files_api.py` 32 passed（唯一失败 `test_upload_file_large_file` 为既有陈旧用例——构造 100MB 而上限恰为 100MB，与本功能无关）；前端 `vitest` 全量 104 passed、`tsc --noEmit` 零错误。
