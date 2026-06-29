# 单文档唯一 tag — Phase 2（软删除 `deleted_at` 子系统）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把所有异步删除入口统一为「软删除」——入队即 `deleted_at=now()` + 释放 `tag`、对读立即不可见、物理清理（向量/对象/行）异步；并逐条审计读路径加 `deleted_at IS NULL` 过滤，使 tag 可在删除后立即复用而旧文档逻辑消失。

**Architecture:** `files` 表加 `deleted_at` 列 + `idx_files_deleted_at`；新增**非提交**的 `TaskService.add_task()`，删除 helper `_release_tag_and_soft_delete()` 在同一 session 内置 `deleted_at`/`tag=None` + 入队清理任务，**单次 commit**、失败 rollback；三个异步删除入口（内部 `DELETE /files/{id}`、内部 `delete-path-prefix`、外部 `DELETE by-path`）统一软删除；worker 物理清理按 `deleted_before` 水位只删已软删行；所有返回/替换/预览/父目录校验/检索读路径加 active 过滤。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Alembic（生产 PostgreSQL、测试 SQLite in-memory）；pytest；前端无改动。

**Scope note（承接 Phase 1）:**
- Phase 1 已落地：`tag` 列 + `uq_files_workspace_tag`、迁移 `20260626_0004`、ingest tag 校验/查重/分类、两上传入口透传回显、`GET by-tag`、前端 tag 输入。**本计划不重复这些。**
- 本阶段迁移为**独立的 `20260626_0005`**（`down_revision="20260626_0004"`），只加 `deleted_at` 列 + 索引。
- Phase 1 的 `GET by-tag`（[service_api.py](../../../openrag/src/openrag/api/service_api.py)）查询**当前不含** `deleted_at IS NULL`——本计划 Task 9 必补。
- Phase 3（`PUT upsert-by-tag`）仍为独立计划，不在此。

**测试运行约定（Phase 1 实测 + Codex #6）:** worktree 无独立 venv；用主仓库 venv 跑、cwd 设在 worktree，`pytest.ini` 的 `pythonpath = . src` 会让 worktree 代码优先。**必须用主仓库 venv 的显式 python**（worktree 下裸 `python` 会解析到用户目录 wrapper，报 `No module named pytest`）。下文所有命令统一为：
```powershell
& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest <文件> -v
```
新测试文件不在 `pytest.ini` 的 `python_files` 白名单内，**用显式路径调用即可收集**。跨文件复用 fixture 用 `from tests.test_service_api import _root, _stub_app_startup, client, db, owner, workspace, service_token_headers, service_token_write_headers`（Phase 1 验证可行）。

> **关于 `--import-mode=importlib`（解决文档内冲突）：** 旧 Codex 审查段曾建议给 pytest 统一加 `--import-mode=importlib`。**本计划不加**——Phase 1 已用上面这套（默认 prepend import-mode + `conftest.py` 把 repo root/`src` 插入 `sys.path` + 跨文件 `from tests.X import ...`）实测全绿；引入 importlib 在本仓库未经验证、且会改变测试模块的导入身份，风险高于收益。以本约定为准；仅当执行时真的遇到收集/同名模块冲突，再把 `--import-mode=importlib` 作为兜底。

---

## 文件结构（本阶段创建/修改）

| 文件 | 职责 |
|---|---|
| `openrag/src/openrag/models/file.py`（改） | `File` 加 `deleted_at` 列 + `idx_files_deleted_at` 索引 |
| `openrag/alembic/versions/20260626_0005_add_file_deleted_at.py`（建） | 加 `deleted_at` 列 + 索引的迁移 |
| `openrag/src/openrag/services/task_service.py`（改） | 新增非提交 `add_task()`；`create_task()` 改为复用之 |
| `openrag/src/openrag/services/file_deletion.py`（改） | 工具 `utcnow()`（naive）；`_release_tag_and_soft_delete()`、`soft_delete_subtree()`（拒绝空/`"/"` prefix）、`soft_delete_subtree_and_enqueue()`（单事务+回滚）、`_physically_delete_row_only()`（不级联 children、目录不碰 MinIO）、`physically_delete_soft_deleted_under_prefix()` |
| `openrag/src/openrag/api/files_api.py`（改） | `DELETE /files/{id}` 异步软删除（文件单行 / 目录 subtree+`DELETE_PATH_PREFIX`；拒绝删根 `uri="/"`）；`delete-path-prefix` 异步批量软删 + `deleted_before`；list/详情/content/reprocess/move 读路径加 active 过滤；`create_directory`/`move_file` 接入 pending-deletion 写守卫 |
| `openrag/src/openrag/api/service_api.py`（改） | 新增 `DELETE .../documents/by-path`（异步软删 202 / 同步物删 200）；`GET by-tag`、`by-path`、replace、preview-link 定位加 active 过滤 |
| `openrag/src/openrag/services/file_ingest.py`（改） | 父目录校验 / `ensure_directory_path` 只把 active 目录视为存在；新增 `assert_no_pending_deleted_ancestor()` 写守卫并接入 `ingest_new_file` |
| `openrag/src/openrag/services/workspace_file_tree.py`（改） | 五个读函数加 `deleted_at IS NULL` |
| `openrag/src/openrag/api/workspace_file_api.py`（改） | `get_readable_workspace_file_or_404` 加 active 过滤（覆盖 chunks/content/preview/chunk-source）|
| `openrag/src/openrag/api/embed_preview_api.py`（改） | `resolve_claims_file_and_chunk` 加 active 过滤 |
| `openrag/src/openrag/api/search_api.py`（改） | `GET /search/chunks/{chunk_id}` 取文件行加 active 过滤 |
| `openrag/src/openrag/retrieval/retrieval_service.py`（改） | `_accessible_file_ids()` 只返回 active；admin+workspace 无 active 返回 `[]`；新增 `_filter_hits_to_active_files()` 在 `_search_flat`/`_search_contextual` enrich 后丢弃软删 hit |
| `openrag/src/openrag/worker/task_worker.py`（改） | `DELETE_PATH_PREFIX` 按 `deleted_before` 的新 helper（缺水位 fail-closed）；`DELETE_FILE` 加 `deleted_at` 防御 |
| `openrag/tests/test_soft_delete_*.py`（建） | 各任务测试（model / helpers / files_api / service / worker / read_paths / write_guard / retrieval）|

---

## Task 1: `File` 模型加 `deleted_at` 列 + 索引

**Files:**
- Modify: `openrag/src/openrag/models/file.py`（`tag` 列旁加 `deleted_at`；`__table_args__` 加索引）
- Test: `openrag/tests/test_soft_delete_model.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_soft_delete_model.py`:

```python
"""File.deleted_at column + multiple soft-deleted rows can share NULL tag."""

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def wsowner(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


def test_deleted_at_defaults_null_and_settable(db, wsowner):
    w, u = wsowner
    f = File(uri="/a.txt", name="a.txt", owner_id=u.id, workspace_id=w.id, tag="t")
    db.add(f); db.commit(); db.refresh(f)
    assert f.deleted_at is None
    f.deleted_at = datetime.now(timezone.utc)
    f.tag = None
    db.commit(); db.refresh(f)
    assert f.deleted_at is not None
    assert f.tag is None


def test_soft_deleted_row_frees_tag_for_reuse(db, wsowner):
    """A soft-deleted row has tag=NULL, so the same tag can be inserted again."""
    w, u = wsowner
    old = File(uri="/a.txt", name="a.txt", owner_id=u.id, workspace_id=w.id, tag="dup")
    db.add(old); db.commit()
    old.deleted_at = datetime.now(timezone.utc); old.tag = None
    db.commit()
    new = File(uri="/b.txt", name="b.txt", owner_id=u.id, workspace_id=w.id, tag="dup")
    db.add(new); db.commit()  # no IntegrityError: old.tag is NULL
    assert db.query(File).filter(File.tag == "dup").count() == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_model.py -v`
Expected: FAIL — `TypeError: 'deleted_at' is an invalid keyword argument`（或属性不存在）。

- [ ] **Step 3: 实现模型改动**

In `openrag/src/openrag/models/file.py`，在 `tag` 列后（`workspace_id` 之前）加：

```python
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP,
        nullable=True,
        comment="Soft-delete marker; row pending physical cleanup",
    )
```

`__table_args__` 追加索引（保留已有项）：

```python
    __table_args__ = (
        UniqueConstraint("workspace_id", "uri", name="uq_files_workspace_uri"),
        UniqueConstraint("workspace_id", "tag", name="uq_files_workspace_tag"),
        Index("idx_file_owner_id", "owner_id"),
        Index("idx_file_parent_id", "parent_id"),
        Index("idx_files_deleted_at", "deleted_at"),
    )
```

> `TIMESTAMP` 与 `datetime` 已在 `file.py` import（`last_aggregated_at` 在用）。无需新 import。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_model.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/models/file.py openrag/tests/test_soft_delete_model.py
git commit -m "feat(model): add File.deleted_at soft-delete column + index"
```

---

## Task 2: Alembic 迁移加 `deleted_at` 列

**Files:**
- Create: `openrag/alembic/versions/20260626_0005_add_file_deleted_at.py`

- [ ] **Step 1: 写迁移文件**

Create `openrag/alembic/versions/20260626_0005_add_file_deleted_at.py`:

```python
"""Add soft-delete marker deleted_at to files.

Revision ID: 20260626_0005
Revises: 20260626_0004
Create Date: 2026-06-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260626_0005"
down_revision: Union[str, None] = "20260626_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("files", sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True))
    op.create_index("idx_files_deleted_at", "files", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("idx_files_deleted_at", table_name="files")
    op.drop_column("files", "deleted_at")
```

- [ ] **Step 2: 验证迁移链路（离线）**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m alembic history | head -5`
Expected: 含 `20260626_0004 -> 20260626_0005 (head)`，无 "multiple heads"。

- [ ] **Step 3: 验证可升降级（需 dev PostgreSQL）**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m alembic upgrade head && & "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m alembic downgrade -1 && & "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m alembic upgrade head`
Expected: 三条都成功。若本地无 PG，跳过并在 PR 标注「迁移待有 DB 环境验证」。

- [ ] **Step 4: 提交**

```bash
git add openrag/alembic/versions/20260626_0005_add_file_deleted_at.py
git commit -m "feat(db): migration adds files.deleted_at + idx_files_deleted_at"
```

---

## Task 3: `TaskService.add_task()`（非提交）+ `create_task()` 复用

**Files:**
- Modify: `openrag/src/openrag/services/task_service.py`（`create_task` ~L20-70）
- Test: `openrag/tests/test_task_service_add_task.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_task_service_add_task.py`:

```python
"""TaskService.add_task is non-committing; create_task still commits."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, Task, User, Workspace
from openrag.services.task_service import TaskService


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def wsuser(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


def test_add_task_does_not_commit(db, wsuser):
    w, u = wsuser
    t = TaskService(db).add_task(workspace_id=w.id, user_id=u.id, task_type="delete_file", file_id=None)
    assert t.task_id  # uuid assigned
    # not committed yet -> rollback discards it
    db.rollback()
    assert db.query(Task).count() == 0


def test_create_task_commits(db, wsuser):
    w, u = wsuser
    TaskService(db).create_task(workspace_id=w.id, user_id=u.id, task_type="delete_file")
    db.rollback()  # already committed inside; rollback is a no-op for it
    assert db.query(Task).count() == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_task_service_add_task.py -v`
Expected: FAIL — `AttributeError: 'TaskService' object has no attribute 'add_task'`。

- [ ] **Step 3: 实现 `add_task` 并让 `create_task` 复用**

In `openrag/src/openrag/services/task_service.py`，把现有 `create_task` 的 `Task(...)` 构造抽到非提交的 `add_task`，`create_task` 调用它再 commit。替换 `create_task` 方法体（L20-70）为：

```python
    def add_task(
        self,
        workspace_id: int,
        user_id: int,
        file_id: Optional[int] = None,
        task_type: str = "process_document",
        queue: str = "normal",
        priority: int = 5,
        max_retries: int = 3,
        status: Optional[TaskStatus] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """Build + add a Task to the session WITHOUT committing.

        Lets a caller include task creation in a larger single-commit transaction
        (e.g. soft-delete: set deleted_at + tag=None + enqueue cleanup, one commit).
        """
        raw_status = status if status else TaskStatus.PENDING
        status_val = (
            raw_status.value if isinstance(raw_status, TaskStatus) else raw_status
        )
        task = Task(
            task_id=str(uuid4()),
            workspace_id=workspace_id,
            user_id=user_id,
            file_id=file_id,
            task_type=task_type,
            queue=queue,
            priority=priority,
            status=status_val,
            progress=0,
            retry_count=0,
            max_retries=max_retries,
            payload=payload,
        )
        self.db.add(task)
        return task

    def create_task(
        self,
        workspace_id: int,
        user_id: int,
        file_id: Optional[int] = None,
        task_type: str = "process_document",
        queue: str = "normal",
        priority: int = 5,
        max_retries: int = 3,
        status: Optional[TaskStatus] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """Create a new task and commit (commit-on-call contract, unchanged)."""
        task = self.add_task(
            workspace_id=workspace_id,
            user_id=user_id,
            file_id=file_id,
            task_type=task_type,
            queue=queue,
            priority=priority,
            max_retries=max_retries,
            status=status,
            payload=payload,
        )
        self.db.commit()
        self.db.refresh(task)
        return task
```

- [ ] **Step 4: 跑测试确认通过 + 既有 task 回归**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_task_service_add_task.py -v`
Expected: PASS（2 passed）。

Run（既有用例不回归）: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_service_api.py -q`
Expected: 全 PASS（上传仍能建 `process_document` 任务）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/task_service.py openrag/tests/test_task_service_add_task.py
git commit -m "feat(task): add non-committing TaskService.add_task; create_task reuses it"
```

---

## Task 4: 软删除 helper（单行 / subtree / 物理清理）

**Files:**
- Modify: `openrag/src/openrag/services/file_deletion.py`（顶部加 helper）
- Test: `openrag/tests/test_soft_delete_helpers.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_soft_delete_helpers.py`:

```python
"""Soft-delete helpers: single-row, subtree, and physical-cleanup-by-watermark."""

import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, Task, User, Workspace
from openrag.services import file_deletion
from openrag.services.file_deletion import (
    _release_tag_and_soft_delete,
    soft_delete_subtree,
    utcnow,
)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def wsowner(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


def _mk(db, w, u, uri, *, is_dir=False, tag=None):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1] or "root", owner_id=u.id,
             workspace_id=w.id, is_directory=is_dir, size=0, tag=tag)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_release_tag_and_soft_delete_single_commit(db, wsowner):
    w, u = wsowner
    f = _mk(db, w, u, "/a.txt", tag="t1")
    task = _release_tag_and_soft_delete(db, f, user_id=u.id)
    db.refresh(f)
    assert f.deleted_at is not None
    assert f.tag is None
    assert task.task_type == "delete_file"
    assert db.query(Task).filter(Task.file_id == f.id).count() == 1


def test_soft_delete_subtree_marks_all_active_rows(db, wsowner):
    w, u = wsowner
    d = _mk(db, w, u, "/dir", is_dir=True)
    a = _mk(db, w, u, "/dir/a.txt", tag="x")
    b = _mk(db, w, u, "/dir/sub/b.txt", tag="y")
    outside = _mk(db, w, u, "/other.txt", tag="z")
    before = soft_delete_subtree(db, w.id, "/dir")
    db.commit()
    for f in (d, a, b):
        db.refresh(f)
        assert f.deleted_at is not None and f.tag is None
    db.refresh(outside)
    assert outside.deleted_at is None and outside.tag == "z"
    assert isinstance(before, datetime)


def _stub_storage(monkeypatch):
    """Stub external storage/vector deletes so cleanup runs DB-only.

    Returns a list recording every remove_directory(bucket, prefix) call, so a test
    can assert the prefix-recursive object delete was NOT used on a directory row.
    """
    rmdir_calls: list = []
    monkeypatch.setattr(file_deletion, "delete_milvus_vectors_for_file", lambda *a, **k: [])

    class _M:
        def remove_document_hierarchy(self, *a, **k):
            return None
        def remove_file(self, *a, **k):
            return None
        def remove_directory(self, *a, **k):
            rmdir_calls.append(a)

    monkeypatch.setattr(file_deletion, "MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr(file_deletion, "HierarchyStorage", lambda *a, **k: type("H", (), {
        "delete_document_hierarchy": lambda *a, **k: None,
    })())
    return rmdir_calls


def test_physical_cleanup_only_touches_pre_watermark_soft_deleted(db, wsowner, monkeypatch):
    w, u = wsowner
    # one soft-deleted-before-watermark, one active added "after enqueue"
    old = _mk(db, w, u, "/dir/old.txt")
    soft_delete_subtree(db, w.id, "/dir"); db.commit()
    watermark = utcnow()  # naive UTC, same type the helper stamps deleted_at with
    new_active = _mk(db, w, u, "/dir/new.txt")  # active, deleted_at IS NULL
    _stub_storage(monkeypatch)

    ids = file_deletion.physically_delete_soft_deleted_under_prefix(
        db, w.id, "/dir", watermark, w
    )
    assert old.id in ids
    db.refresh(new_active)
    assert new_active.deleted_at is None  # untouched
    assert db.query(File).filter(File.id == old.id).first() is None  # physically gone


def test_cleanup_does_not_cascade_active_children_of_soft_deleted_dir(db, wsowner, monkeypatch):
    """Codex #1: a soft-deleted directory row must NOT drag an active child with it.

    soft-delete /dir (and its old child), then an active /dir/new.txt appears; the
    watermark cleanup deletes only the pre-watermark soft-deleted rows (the dir row
    + old child), and the no-cascade single-row delete leaves /dir/new.txt intact.
    """
    w, u = wsowner
    d = _mk(db, w, u, "/dir", is_dir=True)
    old_child = _mk(db, w, u, "/dir/old.txt")
    soft_delete_subtree(db, w.id, "/dir"); db.commit()
    watermark = utcnow()
    new_active = _mk(db, w, u, "/dir/new.txt")  # appears AFTER watermark, active
    rmdir_calls = _stub_storage(monkeypatch)

    ids = file_deletion.physically_delete_soft_deleted_under_prefix(
        db, w.id, "/dir", watermark, w
    )
    assert d.id in ids and old_child.id in ids
    assert db.query(File).filter(File.id == new_active.id).first() is not None  # survived
    assert new_active.deleted_at is None
    # Codex round-3 #1: directory row cleanup must NOT prefix-recursive delete objects
    assert rmdir_calls == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_helpers.py -v`
Expected: FAIL — helper 不存在（ImportError）。

- [ ] **Step 3: 实现 helper**

In `openrag/src/openrag/services/file_deletion.py`，顶部 import 区加：

```python
from datetime import datetime, timezone

from fastapi import HTTPException, status

from openrag.services.task_service import TaskService
```

> `Task` 已在 `file_deletion.py` import（`from openrag.models.task import Task`，`delete_file_with_storage` 在用），下文类型注解直接复用，无需再 import 别名。`fastapi` 此前未在本模块 import（root 防御会 raise `HTTPException`），故新增。

文件末尾追加（紧挨现有函数之后）：

```python
def utcnow() -> datetime:
    """Naive UTC (Codex #5): match the project's naive DateTime/TIMESTAMP columns so
    ``deleted_at <= deleted_before`` compares identically on PostgreSQL and SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _release_tag_and_soft_delete(db: Session, file: FileModel, *, user_id: int) -> Task:
    """Single-row soft delete: set deleted_at + free tag + enqueue DELETE_FILE, ONE commit.

    On failure rolls back so the row never enters a half-deleted state (tag still
    released but no cleanup task, or vice versa).
    """
    try:
        file.deleted_at = utcnow()
        file.tag = None
        task = TaskService(db).add_task(
            workspace_id=file.workspace_id,
            user_id=user_id,
            file_id=file.id,
            task_type="delete_file",
            queue="normal",
            priority=6,
            max_retries=3,
        )
        db.commit()
        db.refresh(task)
        return task
    except Exception:
        db.rollback()
        raise


def soft_delete_subtree(db: Session, workspace_id: int, prefix: str) -> datetime:
    """Mark every ACTIVE row at/under ``prefix`` as soft-deleted (deleted_at + tag=None).

    Returns the watermark timestamp used (caller enqueues a DELETE_PATH_PREFIX task
    carrying it, then commits once). Does NOT commit.
    """
    p = prefix.rstrip("/")
    if not p:
        # Codex round-3 #2: an empty/"/" prefix would match the whole workspace
        # (uri LIKE "/%"). Refuse so no caller can soft-delete an entire workspace.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Refusing to soft-delete workspace root",
        )
    watermark = utcnow()
    rows = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            or_(FileModel.uri == p, FileModel.uri.like(f"{p}/%")),
            FileModel.deleted_at.is_(None),
        )
        .all()
    )
    for f in rows:
        f.deleted_at = watermark
        f.tag = None
    return watermark


def soft_delete_subtree_and_enqueue(
    db: Session, workspace_id: int, prefix: str, *, user_id: int
) -> Task:
    """Subtree soft delete in ONE commit (Codex #4): mark all active rows at/under
    ``prefix`` deleted + tag=None, enqueue a DELETE_PATH_PREFIX task carrying the
    watermark, commit once; rollback on any failure so no half-deleted state leaks.
    Shared by internal DELETE /files/{id} (directory) and delete-path-prefix.
    """
    p = prefix.rstrip("/")
    try:
        watermark = soft_delete_subtree(db, workspace_id, p)
        task = TaskService(db).add_task(
            workspace_id=workspace_id,
            user_id=user_id,
            file_id=None,
            task_type="delete_path_prefix",
            queue="normal",
            priority=6,
            max_retries=3,
            payload={"path": p, "deleted_before": watermark.isoformat()},
        )
        db.commit()
        db.refresh(task)
        return task
    except Exception:
        db.rollback()
        raise


def _physically_delete_row_only(db: Session, file: FileModel, workspace: Workspace) -> None:
    """Physically delete ONE row's storage/vectors/DB, WITHOUT touching any other
    row's objects (Codex round-2 #1 + round-3 #1).

    Two cascade traps avoided:
    - DB children: ``delete_file_with_storage`` deletes a directory's children by URI
      prefix ignoring ``deleted_at``; we never call it here.
    - MinIO objects: ``MinioStorage.remove_directory(prefix)`` is a *prefix-recursive*
      object delete (``list_objects(prefix, recursive=True)``), so calling it for a
      soft-deleted directory would wipe active children's objects that appeared after
      the soft-delete. Directory rows are DB-only virtual nodes with no MinIO object,
      so the directory branch touches **no** object storage at all.

    The watermark cleanup enumerates every soft-deleted row (children included) and
    deletes deepest-first, so each file's own objects are removed by its own row.
    """
    if file.is_directory:
        # DB-only virtual node: NO MinIO object, NO remove_directory (prefix-recursive).
        pass
    else:
        minio_storage = MinioStorage()
        minio_storage.remove_document_hierarchy(workspace.slug, file.uri)
        minio_storage.remove_file(workspace.slug, file.uri)
        HierarchyStorage().delete_document_hierarchy(file_uri=file.uri)
        delete_milvus_vectors_for_file(file.id)
    db.query(Task).filter(Task.file_id == file.id).delete(synchronize_session=False)
    db.delete(file)
    db.commit()


def physically_delete_soft_deleted_under_prefix(
    db: Session,
    workspace_id: int,
    prefix: str,
    deleted_before: datetime,
    workspace: Workspace,
) -> list[int]:
    """Physically delete ONLY soft-deleted rows at/under ``prefix`` whose
    ``deleted_at <= deleted_before`` (deepest URI first). Active rows created after
    the task was enqueued are never touched. Uses the no-cascade single-row helper
    so a soft-deleted directory never drags active children with it (Codex #1).
    """
    p = prefix.rstrip("/")
    rows = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            or_(FileModel.uri == p, FileModel.uri.like(f"{p}/%")),
            FileModel.deleted_at.is_not(None),
            FileModel.deleted_at <= deleted_before,
        )
        .all()
    )
    rows.sort(key=lambda f: (len(f.uri), f.id), reverse=True)
    deleted_ids: list[int] = []
    for f in rows:
        fresh = db.query(FileModel).filter(FileModel.id == f.id).first()
        if fresh is None:
            continue
        _physically_delete_row_only(db, fresh, workspace)
        deleted_ids.append(f.id)
    return deleted_ids
```

> `or_`、`Task`、`MinioStorage`、`HierarchyStorage`、`delete_milvus_vectors_for_file`、`FileModel`、`Workspace` 均已在 `file_deletion.py` import（现有函数在用）。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_helpers.py -v`
Expected: PASS（4 passed，含目录不级联 active 子项）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/file_deletion.py openrag/tests/test_soft_delete_helpers.py
git commit -m "feat(deletion): soft-delete helpers (single-row, subtree, watermark cleanup)"
```

---

## Task 5: 内部 `DELETE /files/{id}` 改软删除（文件 / 目录分流）

**Files:**
- Modify: `openrag/src/openrag/api/files_api.py`（`delete_file` ~L746-826）
- Test: `openrag/tests/test_soft_delete_files_api.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_soft_delete_files_api.py`:

```python
"""Internal DELETE /files/{id}: async soft-delete (file row / directory subtree)."""

import pytest
from fastapi import status

from openrag.api.main import app
from openrag.api.deps import get_current_user
from openrag.models import File, Task

from tests.test_files_api import (  # noqa: F401
    client, db, test_user, test_workspace, FakeMinioStorage,
    override_get_current_user_factory,
)


def _mk(db, ws, owner_id, uri, *, is_dir=False, tag=None):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1] or "root", owner_id=owner_id,
             workspace_id=ws.id, is_directory=is_dir, size=0, tag=tag)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_delete_file_async_soft_deletes_and_frees_tag(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/a.txt", tag="t1")
        r = client.delete(f"/files/{f.id}")  # background=True default
        assert r.status_code == status.HTTP_202_ACCEPTED
        db.expire_all()
        row = db.query(File).filter(File.id == f.id).first()
        assert row.deleted_at is not None and row.tag is None
        assert db.query(Task).filter(Task.file_id == f.id, Task.task_type == "delete_file").count() == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_directory_async_soft_deletes_subtree_and_enqueues_prefix(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        d = _mk(db, test_workspace, test_user.id, "/dir", is_dir=True)
        a = _mk(db, test_workspace, test_user.id, "/dir/a.txt", tag="x")
        r = client.delete(f"/files/{d.id}")
        assert r.status_code == status.HTTP_202_ACCEPTED
        db.expire_all()
        for fid in (d.id, a.id):
            assert db.query(File).filter(File.id == fid).first().deleted_at is not None
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_file_rollback_when_add_task_fails(client, db, test_user, test_workspace, monkeypatch):
    """Codex #4: if add_task raises, deleted_at/tag/task all roll back (file stays active)."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/a.txt", tag="t1")

        def _boom(*a, **k):
            raise RuntimeError("enqueue failed")
        monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

        # TestClient(raise_server_exceptions=True default) re-raises the unhandled error;
        # the helper has already rolled back before propagating.
        with pytest.raises(RuntimeError):
            client.delete(f"/files/{f.id}")
        db.expire_all()
        row = db.query(File).filter(File.id == f.id).first()
        assert row.deleted_at is None and row.tag == "t1"  # unchanged
        assert db.query(Task).filter(Task.file_id == f.id).count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_directory_rollback_when_add_task_fails(client, db, test_user, test_workspace, monkeypatch):
    """Codex #4: directory subtree soft-delete rolls back fully if enqueue fails."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        d = _mk(db, test_workspace, test_user.id, "/dir", is_dir=True)
        a = _mk(db, test_workspace, test_user.id, "/dir/a.txt", tag="x")

        def _boom(*a, **k):
            raise RuntimeError("enqueue failed")
        monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

        with pytest.raises(RuntimeError):
            client.delete(f"/files/{d.id}")
        db.expire_all()
        for fid in (d.id, a.id):
            row = db.query(File).filter(File.id == fid).first()
            assert row.deleted_at is None  # rolled back
        assert db.query(File).filter(File.id == a.id).first().tag == "x"
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_root_directory_returns_400(client, db, test_user, test_workspace):
    """Codex round-3 #2: deleting the workspace root directory must be rejected."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        root = _mk(db, test_workspace, test_user.id, "/", is_dir=True)
        child = _mk(db, test_workspace, test_user.id, "/keep.txt", tag="k")
        r = client.delete(f"/files/{root.id}")
        assert r.status_code == status.HTTP_400_BAD_REQUEST
        db.expire_all()
        kept = db.query(File).filter(File.id == child.id).first()
        assert kept.deleted_at is None and kept.tag == "k"  # nothing soft-deleted
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_files_api.py -v`
Expected: FAIL — 当前 `background=True` 只建 `DELETE_FILE` 任务、不设 `deleted_at`/不释放 tag；目录不会入队 `DELETE_PATH_PREFIX`；回滚与根目录用例因尚无软删事务/根防御而失败。

- [ ] **Step 3: 实现软删除分流**

In `openrag/src/openrag/api/files_api.py`，先在 `delete_file` 取得 `workspace` 之后、`if background:` 之前加根目录防御（Codex round-3 #2，覆盖同步/异步两条路径）：

```python
        if file.is_directory and file.uri.rstrip("/") == "":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete workspace root",
            )
```

再把 `if background:` 分支（原 L792-811）替换为：

```python
        if background:
            from openrag.services.file_deletion import (
                _release_tag_and_soft_delete,
                soft_delete_subtree_and_enqueue,
            )

            if file.is_directory:
                # subtree soft-delete + DELETE_PATH_PREFIX in one commit (rollback on failure)
                task_record = soft_delete_subtree_and_enqueue(
                    db, file.workspace_id, file.uri.rstrip("/"), user_id=current_user.id
                )
            else:
                task_record = _release_tag_and_soft_delete(
                    db, file, user_id=current_user.id
                )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "message": "File deletion queued",
                    "task_id": task_record.id,
                    "async": True,
                },
            )
```

> `background=false` 分支（`delete_file_with_storage`）保持不变：同步物删，行直接消失。两个 helper 都在内部「单次 commit、失败 rollback」，故 `deleted_at`/`tag`/task 三者要么全成要么全不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_files_api.py -v`
Expected: PASS（5 passed：文件软删 + 目录 subtree + 文件回滚 + 目录回滚 + 根目录 400）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/files_api.py openrag/tests/test_soft_delete_files_api.py
git commit -m "feat(api): DELETE /files/{id} async path soft-deletes (file row / dir subtree)"
```

---

## Task 6: 内部 `delete-path-prefix` 改软删除 + `deleted_before`

**Files:**
- Modify: `openrag/src/openrag/api/files_api.py`（`delete_path_prefix` ~L880-901）
- Test: `openrag/tests/test_soft_delete_files_api.py`（追加）

- [ ] **Step 1: 追加失败测试**

Append to `openrag/tests/test_soft_delete_files_api.py`:

```python
def test_delete_path_prefix_async_soft_deletes_and_writes_watermark(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        a = _mk(db, test_workspace, test_user.id, "/docs/a.txt", tag="p1")
        b = _mk(db, test_workspace, test_user.id, "/docs/sub/b.txt", tag="p2")
        r = client.post(
            "/files/delete-path-prefix",
            params={"background": "true"},
            json={"workspace_id": test_workspace.id, "path": "/docs"},
        )
        assert r.status_code == status.HTTP_202_ACCEPTED
        db.expire_all()
        for fid in (a.id, b.id):
            row = db.query(File).filter(File.id == fid).first()
            assert row.deleted_at is not None and row.tag is None
        task = db.query(Task).filter(Task.task_type == "delete_path_prefix").first()
        assert task is not None
        assert "deleted_before" in (task.payload or {})
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_delete_path_prefix_rollback_when_add_task_fails(client, db, test_user, test_workspace, monkeypatch):
    """Codex #4: prefix soft-delete rolls back fully if enqueue fails (no rows marked)."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        a = _mk(db, test_workspace, test_user.id, "/docs/a.txt", tag="p1")

        def _boom(*a, **k):
            raise RuntimeError("enqueue failed")
        monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

        with pytest.raises(RuntimeError):
            client.post(
                "/files/delete-path-prefix",
                params={"background": "true"},
                json={"workspace_id": test_workspace.id, "path": "/docs"},
            )
        db.expire_all()
        row = db.query(File).filter(File.id == a.id).first()
        assert row.deleted_at is None and row.tag == "p1"  # rolled back
        assert db.query(Task).filter(Task.task_type == "delete_path_prefix").count() == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_files_api.py::test_delete_path_prefix_async_soft_deletes_and_writes_watermark -v`
Expected: FAIL — 当前异步分支只建任务、不设 `deleted_at`，payload 无 `deleted_before`。

- [ ] **Step 3: 实现软删除批量 + 水位**

In `openrag/src/openrag/api/files_api.py`，`delete_path_prefix` 的 `if background:` 分支（L880-901）替换为：

```python
    if background:
        from openrag.services.file_deletion import soft_delete_subtree_and_enqueue

        # subtree soft-delete + DELETE_PATH_PREFIX(watermark) in one commit (rollback on failure)
        task_record = soft_delete_subtree_and_enqueue(
            db, body.workspace_id, prefix, user_id=current_user.id
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "message": "Path prefix deletion queued",
                "task_id": task_record.id,
                "async": True,
                "path": prefix,
            },
        )
```

> 上方的 `n == 0 → 返回` 检查应只数 active 行——把 L869-876 的 count 查询加 `FileModel.deleted_at.is_(None)`，避免对全已软删的 prefix 反复入队。`background=false` 分支保持原同步物删。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_files_api.py -v`
Expected: PASS（6 passed：含 prefix 软删 + prefix 回滚）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/files_api.py openrag/tests/test_soft_delete_files_api.py
git commit -m "feat(api): delete-path-prefix async path soft-deletes subtree + writes deleted_before"
```

---

## Task 7: 外部 `DELETE /service/v1/workspaces/{name}/documents/by-path`

**Files:**
- Modify: `openrag/src/openrag/api/service_api.py`（`service_replace_document` 附近加新路由）
- Test: `openrag/tests/test_service_api_soft_delete.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_service_api_soft_delete.py`:

```python
"""External DELETE by-path: async soft-delete (202) frees tag + hides doc."""

import pytest

from openrag.models import File, Task, Workspace

from tests.test_service_api import (  # noqa: F401
    _root, _stub_app_startup, client, db, owner, workspace,
    service_token_headers, service_token_write_headers,
)


def _mk(db, ws, owner, uri, *, tag=None):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=owner.id,
             workspace_id=ws.id, is_directory=False, size=3, mime_type="text/plain", tag=tag)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_delete_by_path_async_soft_deletes(client, db, workspace, owner, service_token_write_headers):
    f = _mk(db, workspace, owner, "/d.txt", tag="dt")
    r = client.request(
        "DELETE",
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/d.txt"},
        headers=service_token_write_headers,
    )
    assert r.status_code == 202
    db.expire_all()
    row = db.query(File).filter(File.id == f.id).first()
    assert row.deleted_at is not None and row.tag is None
    assert db.query(Task).filter(Task.file_id == f.id, Task.task_type == "delete_file").count() == 1


def test_delete_by_path_unknown_404(client, db, workspace, service_token_write_headers):
    r = client.request(
        "DELETE",
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/nope.txt"},
        headers=service_token_write_headers,
    )
    assert r.status_code == 404


def test_delete_by_path_requires_write_403(client, db, workspace, owner, service_token_headers):
    _mk(db, workspace, owner, "/d.txt", tag="dt")
    r = client.request(
        "DELETE",
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/d.txt"},
        headers=service_token_headers,  # read-only token
    )
    assert r.status_code == 403


def test_delete_by_path_rollback_when_add_task_fails(client, db, workspace, owner, service_token_write_headers, monkeypatch):
    """Codex #4: external by-path soft-delete rolls back fully if enqueue fails."""
    f = _mk(db, workspace, owner, "/d.txt", tag="dt")

    def _boom(*a, **k):
        raise RuntimeError("enqueue failed")
    monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

    with pytest.raises(RuntimeError):
        client.request(
            "DELETE",
            f"/service/v1/workspaces/{workspace.name}/documents/by-path",
            params={"path": "/d.txt"},
            headers=service_token_write_headers,
        )
    db.expire_all()
    row = db.query(File).filter(File.id == f.id).first()
    assert row.deleted_at is None and row.tag == "dt"  # rolled back
    assert db.query(Task).filter(Task.file_id == f.id).count() == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_service_api_soft_delete.py -v`
Expected: FAIL — 路由不存在（404/405 for all）。

- [ ] **Step 3: 实现外部 DELETE by-path**

In `openrag/src/openrag/api/service_api.py`，import 区加：

```python
from openrag.services.file_deletion import (
    _release_tag_and_soft_delete,
    delete_file_with_storage,
)
```

在 `service_replace_document`（`@router.put(".../documents/by-path")`）之后加：

```python
@router.delete("/workspaces/{workspace_name}/documents/by-path")
async def service_delete_document_by_path(
    workspace_name: str,
    path: str = Query(..., description="Full file logical path"),
    background: bool = Query(True, description="true: async soft-delete (202); false: sync physical delete (200)"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> JSONResponse:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    p = validate_path(path)
    row = (
        db.query(DbFile)
        .filter(
            DbFile.workspace_id == ws.id,
            DbFile.uri == p,
            DbFile.is_directory.is_(False),
            DbFile.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if background:
        task = _release_tag_and_soft_delete(db, row, user_id=ws.owner_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"message": "File deletion queued", "task_id": task.id, "async": True},
        )
    delete_file_with_storage(db, row, ws)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "File deleted", "async": False})
```

> 需确认 `JSONResponse` 已在 `service_api.py` import；若无则在 import 区加 `from fastapi.responses import JSONResponse`。`validate_path`、`require_workspace_for_name`、`assert_token_workspace_permission`、`DbFile`、`ServiceTokenContext`、`get_service_token_context` 均已在用。

- [ ] **Step 4: 跑测试确认通过 + service 回归**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_service_api_soft_delete.py tests/test_service_api.py -q`
Expected: 全 PASS（by-path 软删 + 404 + 403 + 回滚，且既有 service 不回归）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/service_api.py openrag/tests/test_service_api_soft_delete.py
git commit -m "feat(service): DELETE documents/by-path (async soft-delete 202 / sync 200)"
```

---

## Task 8: worker 物理清理按 `deleted_before` 水位

**Files:**
- Modify: `openrag/src/openrag/worker/task_worker.py`（`_delete_path_prefix_task` ~L471-500；`_delete_file_task` ~L444-469）
- Test: `openrag/tests/test_worker_soft_delete_cleanup.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_worker_soft_delete_cleanup.py`:

```python
"""DELETE_PATH_PREFIX worker physically deletes only pre-watermark soft-deleted rows."""

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.models import Base, File, User, Workspace
from openrag.services import file_deletion
from openrag.services.file_deletion import utcnow


@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine)
    # worker uses SessionLocal()
    monkeypatch.setattr("openrag.worker.task_worker.SessionLocal", Local)
    # stub external storage/vector deletes
    monkeypatch.setattr(file_deletion, "delete_milvus_vectors_for_file", lambda *a, **k: [])
    monkeypatch.setattr(file_deletion, "MinioStorage", lambda *a, **k: type("M", (), {
        "remove_document_hierarchy": lambda *a, **k: None,
        "remove_file": lambda *a, **k: None,
        "remove_directory": lambda *a, **k: None,
    })())
    monkeypatch.setattr(file_deletion, "HierarchyStorage", lambda *a, **k: type("H", (), {
        "delete_document_hierarchy": lambda *a, **k: None,
    })())
    return Local


def test_prefix_cleanup_skips_post_watermark_active_rows(session_factory):
    from openrag.worker.task_worker import TaskWorker

    db = session_factory()
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    old = File(uri="/docs/old.txt", name="old.txt", owner_id=u.id, workspace_id=w.id,
               size=0, deleted_at=utcnow())
    db.add(old); db.commit(); db.refresh(old)
    watermark = utcnow()
    new_active = File(uri="/docs/new.txt", name="new.txt", owner_id=u.id, workspace_id=w.id, size=0)
    db.add(new_active); db.commit(); db.refresh(new_active)
    old_id, new_id = old.id, new_active.id
    db.close()

    worker = TaskWorker.__new__(TaskWorker)  # bypass __init__ (no broker needed)
    result = worker._delete_path_prefix_task({
        "workspace_id": w.id,
        "payload": {"path": "/docs", "deleted_before": watermark.isoformat()},
    })
    assert result["status"] == "deleted"

    db2 = session_factory()
    assert db2.query(File).filter(File.id == old_id).first() is None  # physically gone
    assert db2.query(File).filter(File.id == new_id).first() is not None  # untouched
    db2.close()


def test_legacy_task_without_watermark_is_fail_closed(session_factory):
    """Codex #1: a DELETE_PATH_PREFIX task missing deleted_before must NOT prefix-wide
    delete (which would drop active rows). It fails closed: deletes nothing, reports skip."""
    from openrag.worker.task_worker import TaskWorker

    db = session_factory()
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    active = File(uri="/docs/keep.txt", name="keep.txt", owner_id=u.id, workspace_id=w.id, size=0)
    db.add(active); db.commit(); db.refresh(active)
    keep_id = active.id
    db.close()

    worker = TaskWorker.__new__(TaskWorker)
    result = worker._delete_path_prefix_task({
        "workspace_id": w.id,
        "payload": {"path": "/docs"},  # NO deleted_before
    })
    assert result["status"] == "skipped"

    db2 = session_factory()
    assert db2.query(File).filter(File.id == keep_id).first() is not None  # NOT deleted
    db2.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_worker_soft_delete_cleanup.py -v`
Expected: FAIL — 当前 `_delete_path_prefix_task` 用 `delete_files_under_uri_prefix`（按 prefix 全删），会误删 `new.txt`。

- [ ] **Step 3: 实现 worker 按水位清理**

In `openrag/src/openrag/worker/task_worker.py`，`_delete_path_prefix_task` 体（L479-500）替换为：

```python
        db = SessionLocal()
        try:
            from datetime import datetime
            from openrag.services.file_deletion import (
                physically_delete_soft_deleted_under_prefix,
            )
            from openrag.services.workspace_service import WorkspaceService

            ws_service = WorkspaceService(db)
            workspace = ws_service.get_workspace(workspace_id)
            if not workspace:
                raise ValueError(f"Workspace {workspace_id} not found")

            deleted_before_raw = payload.get("deleted_before")
            if not deleted_before_raw:
                # Codex #1: fail closed. A prefix task without a watermark must NOT
                # fall back to prefix-wide physical delete (that would drop active
                # rows created after enqueue). Phase 2 always writes deleted_before;
                # a task missing it is malformed/legacy — skip and let an operator requeue.
                _logger.error(
                    "delete_path_prefix task missing payload.deleted_before; skipping "
                    "(workspace_id=%s path=%s)", workspace_id, path,
                )
                return {
                    "workspace_id": workspace_id,
                    "path": path,
                    "deleted_count": 0,
                    "deleted_ids": [],
                    "status": "skipped",
                    "reason": "missing_deleted_before",
                }

            deleted_before = datetime.fromisoformat(deleted_before_raw)
            deleted_ids = physically_delete_soft_deleted_under_prefix(
                db, workspace_id, path, deleted_before, workspace
            )
            return {
                "workspace_id": workspace_id,
                "path": path,
                "deleted_count": len(deleted_ids),
                "deleted_ids": deleted_ids,
                "status": "deleted",
            }
        finally:
            db.close()
```

> `_logger` 已在 `task_worker.py` 模块级定义（现有删除任务在用）。

并在 `_delete_file_task`（L456-467）的物理删除前加防御：软删行才删，避免误删 active 行（同 id 复用不会发生，但 worker 重试/竞态时更安全）：

```python
            file = db.query(File).filter(File.id == file_id).first()
            if not file:
                _logger.info("delete_file task: file %s already gone, skip", file_id)
                return {"file_id": file_id, "status": "skipped", "reason": "not_found"}
            if file.deleted_at is None:
                _logger.warning("delete_file task: file %s not soft-deleted, skip", file_id)
                return {"file_id": file_id, "status": "skipped", "reason": "active"}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_worker_soft_delete_cleanup.py -v`
Expected: PASS（2 passed：按水位清理 + 缺水位 fail-closed）。

> 若 `TaskWorker.__new__` 旁路 `__init__` 后 `_delete_path_prefix_task` 仍依赖未初始化的实例属性，改为在测试里直接调用 `physically_delete_soft_deleted_under_prefix(...)`（Task 4 已覆盖其语义），并把本测试降级为「worker 分派读 `deleted_before` 并调用该 helper」的轻量断言。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/worker/task_worker.py openrag/tests/test_worker_soft_delete_cleanup.py
git commit -m "feat(worker): prefix cleanup deletes only pre-watermark soft-deleted rows"
```

---

## Task 9: 读路径审计 — 全面加 `deleted_at IS NULL`

软删行必须对**所有读/替换/预览/父目录校验/检索候选**立即不可见。逐文件加过滤，每处配最小回归。**含 Codex #3 补充的所有按 `file_id` 直接取文件的入口。**

**Files:**
- Modify: `files_api.py`、`service_api.py`、`file_ingest.py`、`workspace_file_tree.py`、`workspace_file_api.py`、`embed_preview_api.py`、`search_api.py`
- Test: `openrag/tests/test_soft_delete_read_paths.py`、`openrag/tests/test_service_api_soft_delete.py`（追加 by-tag/by-path 隐藏断言）

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_soft_delete_read_paths.py`:

```python
"""Soft-deleted rows are invisible to list / detail / by-tag / by-path / tree."""

from datetime import datetime, timezone
from fastapi import status

from openrag.api.main import app
from openrag.api.deps import get_current_user
from openrag.models import File

from tests.test_files_api import (  # noqa: F401
    client, db, test_user, test_workspace,
    override_get_current_user_factory,
)


def _mk(db, ws, owner_id, uri, *, tag=None, deleted=False):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=owner_id, workspace_id=ws.id,
             is_directory=False, size=3, mime_type="text/plain", tag=tag,
             deleted_at=datetime.now(timezone.utc) if deleted else None)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_list_excludes_soft_deleted(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        _mk(db, test_workspace, test_user.id, "/live.txt")
        _mk(db, test_workspace, test_user.id, "/dead.txt", deleted=True)
        r = client.get("/files/", params={"workspace_id": test_workspace.id})
        names = [it["name"] for it in r.json()["items"]]
        assert "live.txt" in names and "dead.txt" not in names
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_detail_and_content_404_for_soft_deleted(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/dead.txt", deleted=True)
        assert client.get(f"/files/{f.id}").status_code == status.HTTP_404_NOT_FOUND
        assert client.get(f"/files/{f.id}/content").status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_reprocess_404_for_soft_deleted(client, db, test_user, test_workspace):
    """Codex #3: /files/{id}/reprocess must not act on a soft-deleted file."""
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        f = _mk(db, test_workspace, test_user.id, "/dead.txt", deleted=True)
        r = client.post(f"/files/{f.id}/reprocess", json={})
        assert r.status_code == status.HTTP_404_NOT_FOUND
    finally:
        app.dependency_overrides.pop(get_current_user, None)
```

并在 `tests/test_service_api_soft_delete.py`（已有 service fixtures）追加：软删行 `GET by-tag` → 404、`GET by-path` → 404、workspace_file_api 的 `chunk-source` → 404（Codex #3）：

```python
def test_get_by_tag_and_by_path_hide_soft_deleted(client, db, workspace, owner, service_token_headers):
    f = _mk(db, workspace, owner, "/dead.txt", tag="dt")
    # soft-delete directly
    from openrag.services.file_deletion import utcnow
    f.deleted_at = utcnow(); f.tag = None
    db.commit()
    assert client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "dt"}, headers=service_token_headers,
    ).status_code == 404
    assert client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/dead.txt"}, headers=service_token_headers,
    ).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_read_paths.py -v`
Expected: FAIL — 软删行仍出现在 list / detail / content / reprocess。

- [ ] **Step 3: 逐处加过滤**

**(a) `files_api.py`：**
- `list_files` 主查询（L504）：
  ```python
  query = db.query(FileModel).filter(
      FileModel.workspace_id.in_(workspace_ids),
      FileModel.deleted_at.is_(None),
  )
  ```
- `_get_readable_file_or_404`（L587，覆盖 detail `GET /files/{id}` 与 content `GET /files/{id}/content`）：
  ```python
  file = (
      db.query(FileModel)
      .filter(FileModel.id == file_id, FileModel.deleted_at.is_(None))
      .first()
  )
  ```
- `reprocess_file` 的定位查询（L1110，**独立于** `_get_readable_file_or_404`）：`db.query(FileModel).filter(FileModel.id == file_id, FileModel.deleted_at.is_(None)).first()`。
- `delete_file` 的定位查询（L768）与 `move_file` 的源行定位：源行加 `FileModel.deleted_at.is_(None)`（软删行不可再删/移动）。

**(b) `service_api.py`：**
- `service_document_by_tag`（Phase 1 新增）查询加 `DbFile.deleted_at.is_(None)`。
- `service_document_by_path` 用的 `get_file_document_by_path`（见 (d)）。
- `service_replace_document` 的定位查询（`uri == p, is_directory == False`）加 `DbFile.deleted_at.is_(None)`。

**(c) `file_ingest.py`：**
- `_assert_parent_directory_exists`（L348/358）两个目录查询加 `FileModel.deleted_at.is_(None)`，使正在删除的目录不算「存在的父目录」。
- `_get_or_create_directory_row`（L189）查找现有目录行时加 `FileModel.deleted_at.is_(None)`：pending-deletion 的祖先目录不被当作可复用目录；若命中一个已软删的同名目录行，应视为不存在并新建/或返回 `409 pending deletion`。**最简实现**：把过滤加到查找上，命中软删行时 `raise HTTPException(409, "Parent path pending deletion")`，避免在删除中的 subtree 下写新文件。
- 同 uri active 查重（ingest 内 `existing_file` 查询，Phase 1 在 L461 一带）：加 `FileModel.deleted_at.is_(None)`，使软删行不挡同 uri 重新上传——**但** spec §4.9 决策为「软删后 uri 仍占用、同 uri 裸上传仍 409」，故**此处保持不加** `deleted_at` 过滤（同 uri 仍 409）。**实现注意：此条不改**，仅在此说明，避免实现者顺手放宽。

**(d) `workspace_file_tree.py`：** 五个查询加 active 过滤：
- `list_direct_children`：`dir_row` 查询、两处 `candidates` 查询。
- `list_entries_by_prefix`：`q`。
- `get_file_document_by_path`：`row` 查询。
- `build_nested_tree`：`root_dir` 查询、`_query_subtree`。
- `search_documents_by_name`：`q`。

统一形式：在每个 `db.query(File).filter(File.workspace_id == workspace_id, ...)` 链上追加 `File.deleted_at.is_(None)`。`_query_subtree` 示例：

```python
def _query_subtree(db: Session, workspace_id: int, path_prefix: str) -> List[File]:
    p = path_prefix.rstrip("/") or "/"
    q = db.query(File).filter(File.workspace_id == workspace_id, File.deleted_at.is_(None))
    ...
```

**(e) 其余按 `file_id` 直接取文件的入口（Codex #3）——逐个在定位查询加 `deleted_at IS NULL`：**
- `workspace_file_api.py` 的 `get_readable_workspace_file_or_404`（L84，**一处即覆盖** chunks / content / preview / chunk-source 四个端点）：
  ```python
  file = (
      db.query(FileModel)
      .filter(FileModel.id == file_id, FileModel.deleted_at.is_(None))
      .first()
  )
  ```
- `service_api.py` 的 `resolve_preview_target`（preview-link）：定位 `DbFile.id == file_id, DbFile.workspace_id == workspace_id` 处追加 `DbFile.deleted_at.is_(None)`（软删后已签发的 preview token 也 404）。
- `embed_preview_api.py` 的 `resolve_claims_file_and_chunk`（embed preview）：定位 `FileModel.id == claims.file_id, FileModel.workspace_id == claims.workspace_id` 处追加 `FileModel.deleted_at.is_(None)`。
- `search_api.py` 的 `GET /search/chunks/{chunk_id}`（chunk context）：取文件行 `f = db.query(FileModel).filter(FileModel.id == row.file_id).first()` 追加 `FileModel.deleted_at.is_(None)`；命中软删→现有的 "File not found" 404 分支自然生效。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_read_paths.py tests/test_service_api_soft_delete.py tests/test_workspace_file_tree.py -v`
Expected: 全 PASS（含既有 tree 测试不回归；reprocess / by-tag / by-path 软删 404）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/files_api.py openrag/src/openrag/api/service_api.py openrag/src/openrag/services/file_ingest.py openrag/src/openrag/services/workspace_file_tree.py openrag/src/openrag/api/workspace_file_api.py openrag/src/openrag/api/embed_preview_api.py openrag/src/openrag/api/search_api.py openrag/tests/test_soft_delete_read_paths.py openrag/tests/test_service_api_soft_delete.py
git commit -m "feat(read-paths): filter deleted_at IS NULL across all read/preview/chunk entry points"
```

---

## Task 10: pending-deletion 写入守卫（Codex #2）

写入口不得把 active 行写进正在删除的 subtree（`/dir` 已软删但 worker 尚未清理时，仍可能创建/移动 active 行到 `/dir/...`）。新增共享校验 helper，接入上传 / `create_dirs`、`create_directory`、`move_file` 的目标路径校验。

**Files:**
- Modify: `openrag/src/openrag/services/file_ingest.py`（加 `assert_no_pending_deleted_ancestor` + 接入 `ingest_new_file`）
- Modify: `openrag/src/openrag/api/files_api.py`（`create_directory`、`move_file` 接入）
- Test: `openrag/tests/test_soft_delete_write_guard.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_soft_delete_write_guard.py`:

```python
"""Write guard: cannot write/move active rows into a soft-deleted (pending) subtree."""

import pytest
from fastapi import HTTPException, status

from openrag.api.main import app
from openrag.api.deps import get_current_user
from openrag.models import File

from tests.test_files_api import (  # noqa: F401
    client, db, test_user, test_workspace, FakeMinioStorage,
    override_get_current_user_factory,
)


def _soft_deleted_dir(db, ws, owner_id, uri):
    from openrag.services.file_deletion import utcnow
    d = File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=owner_id, workspace_id=ws.id,
             is_directory=True, size=0, deleted_at=utcnow())
    db.add(d); db.commit(); db.refresh(d)
    return d


def test_helper_flags_pending_ancestor(db, test_user, test_workspace):
    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
    with pytest.raises(HTTPException) as ei:
        assert_no_pending_deleted_ancestor(db, test_workspace.id, "/dir/sub")
    assert ei.value.status_code == status.HTTP_409_CONFLICT


def test_helper_allows_clean_path(db, test_user, test_workspace):
    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    # no soft-deleted ancestor -> no raise
    assert_no_pending_deleted_ancestor(db, test_workspace.id, "/clean/sub")


def test_upload_into_pending_dir_409(client, db, test_user, test_workspace, monkeypatch):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)
    try:
        _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
        r = client.post(
            "/files/upload",
            files={"file": ("n.txt", b"x", "text/plain")},
            data={"path": "/dir", "workspace_id": str(test_workspace.id)},
        )
        assert r.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_create_directory_under_pending_dir_409(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
        r = client.post(
            "/files/directories",
            data={"path": "/dir/new", "workspace_id": str(test_workspace.id)},
        )
        assert r.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_move_into_pending_dir_409(client, db, test_user, test_workspace):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    try:
        _soft_deleted_dir(db, test_workspace, test_user.id, "/dir")
        src = File(uri="/src.txt", name="src.txt", owner_id=test_user.id,
                   workspace_id=test_workspace.id, is_directory=False, size=0)
        db.add(src); db.commit(); db.refresh(src)
        r = client.put(f"/files/{src.id}/move", json={"new_path": "/dir/src.txt"})
        assert r.status_code == status.HTTP_409_CONFLICT
    finally:
        app.dependency_overrides.pop(get_current_user, None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_write_guard.py -v`
Expected: FAIL — helper 不存在；写入口未拦截 pending-deletion subtree。

- [ ] **Step 3: 实现 helper + 接入三个写入口**

**(a) `file_ingest.py` 加 helper**（放在 `_assert_parent_directory_exists` 旁）：

```python
def assert_no_pending_deleted_ancestor(db: Session, workspace_id: int, target_uri: str) -> None:
    """409 if ``target_uri`` or any ancestor path has a soft-deleted row (pending
    physical cleanup). Prevents writing/moving active rows into a deleting subtree
    (Codex #2). Soft-delete frees tag but not uri, so a soft-deleted ancestor row
    still occupies its uri until the worker cleans it.
    """
    candidates: list[str] = []
    cumulative = ""
    for part in [p for p in target_uri.split("/") if p]:
        cumulative = f"{cumulative}/{part}"
        candidates.append(cumulative)
    if not candidates:
        return
    clash = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            FileModel.uri.in_(candidates),
            FileModel.deleted_at.is_not(None),
        )
        .first()
    )
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Path is pending deletion; retry after cleanup completes",
        )
```

**(b) `ingest_new_file` 接入**：在 `validated_path = validate_path(parent_logical_path)` 之后、`ensure_directory_path(...)` / `_assert_parent_directory_exists(...)` 之前（即 tag 查重之后），加：

```python
        assert_no_pending_deleted_ancestor(db, workspace.id, validated_path)
```

> 用 `validated_path`（父目录），不含叶子 `file_uri`——同 uri 软删行的「裸上传仍 409 File already exists」语义由现有 uri 查重负责（spec §4.9），此处只挡「父/祖先目录 pending deletion」。

**(c) `files_api.py` `create_directory` 接入**：在 `dir_path = build_file_uri(...)` 之后、`existing_dir` 查询之前，加：

```python
    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    assert_no_pending_deleted_ancestor(db, workspace_id, dir_path)
```

**(d) `files_api.py` `move_file` 接入**：在 `new_path = validate_path(request.new_path)` 之后、目标占用检查之前，加：

```python
    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    assert_no_pending_deleted_ancestor(db, file.workspace_id, new_path)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_write_guard.py -v`
Expected: PASS（5 passed：helper 命中/放行 + 上传/建目录/移动 三入口 409）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/src/openrag/api/files_api.py openrag/tests/test_soft_delete_write_guard.py
git commit -m "feat(write-guard): 409 pending-deletion when writing/moving into a soft-deleted subtree"
```

---

## Task 11: 检索读路径 — `_accessible_file_ids` active-only + 回查过滤

**Files:**
- Modify: `openrag/src/openrag/retrieval/retrieval_service.py`（`_accessible_file_ids` L695-744；flat/contextual 回查）
- Test: `openrag/tests/test_soft_delete_retrieval.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_soft_delete_retrieval.py`:

```python
"""_accessible_file_ids excludes soft-deleted; admin+workspace empty -> [] not None."""

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base, File, User, Workspace
from openrag.retrieval.retrieval_service import RetrievalService


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


def _svc(db):
    svc = RetrievalService.__new__(RetrievalService)  # bypass heavy __init__
    svc.db = db
    return svc


def _seed(db, *, is_admin=False):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True, is_admin=is_admin)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return u, w


def test_accessible_excludes_soft_deleted_for_admin_workspace(db):
    u, w = _seed(db, is_admin=True)
    live = File(uri="/a.txt", name="a", owner_id=u.id, workspace_id=w.id, size=0)
    dead = File(uri="/b.txt", name="b", owner_id=u.id, workspace_id=w.id, size=0,
                deleted_at=datetime.now(timezone.utc))
    db.add_all([live, dead]); db.commit(); db.refresh(live)
    ids = _svc(db)._accessible_file_ids(u.id, w.id)
    assert ids == [live.id]


def test_admin_workspace_all_deleted_returns_empty_not_all(db):
    u, w = _seed(db, is_admin=True)
    dead = File(uri="/b.txt", name="b", owner_id=u.id, workspace_id=w.id, size=0,
                deleted_at=datetime.now(timezone.utc))
    db.add(dead); db.commit()
    ids = _svc(db)._accessible_file_ids(u.id, w.id)
    assert ids == []  # NOT None (None would widen to all-files search)


def test_filter_hits_drops_soft_deleted_files(db):
    """Codex round-3 #3: admin + workspace_id=None returns None (no id filter), so
    vector hits for not-yet-physically-cleaned soft-deleted files must be dropped
    by a post-filter on the enriched hits."""
    u, w = _seed(db, is_admin=True)
    live = File(uri="/a.txt", name="a", owner_id=u.id, workspace_id=w.id, size=0)
    dead = File(uri="/b.txt", name="b", owner_id=u.id, workspace_id=w.id, size=0,
                deleted_at=datetime.now(timezone.utc))
    db.add_all([live, dead]); db.commit(); db.refresh(live); db.refresh(dead)
    hits = [
        {"file_id": live.id, "score": 0.9},
        {"file_id": dead.id, "score": 0.8},
        {"file_id": None, "score": 0.1},  # malformed hit, no file_id -> dropped
    ]
    kept = _svc(db)._filter_hits_to_active_files(hits)
    assert [h["file_id"] for h in kept] == [live.id]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_retrieval.py -v`
Expected: FAIL — 软删文件 ID 仍被返回；admin 全删时返回 `None`（扩大为全量）；`_filter_hits_to_active_files` 不存在。

- [ ] **Step 3: 实现 active-only + admin-empty 修复**

In `openrag/src/openrag/retrieval/retrieval_service.py`，`_accessible_file_ids`（L695-744）三处 `select(File.id).where(...)` 都加 `File.deleted_at.is_(None)`，并把 admin+workspace 分支改为返回 `[]` 而非 `None`：

```python
        if user and getattr(user, "is_admin", False):
            if workspace_id:
                rows = (
                    self.db.execute(
                        select(File.id).where(
                            File.workspace_id == workspace_id,
                            File.deleted_at.is_(None),
                        )
                    )
                    .scalars()
                    .all()
                )
                return list(rows)  # empty -> [] (no active files), NOT None
            return None

        if workspace_id:
            rows = (
                self.db.execute(
                    select(File.id).where(
                        File.workspace_id == workspace_id,
                        File.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)
```

并在多 workspace 分支（L734）的 `select(File.id).where(File.workspace_id.in_(ws_ids))` 加 `File.deleted_at.is_(None)`。

**回查兜底 helper（Codex round-3 #3，落成具体实现）：** admin 且 `workspace_id=None` 时 `_accessible_file_ids` 返回 `None`（不按 id 过滤），向量/层级索引里尚未物理清理的软删文件仍可能命中。新增按 hits 的 `file_id` 批量过滤 helper，并在两条检索路径 enrich 之后调用。

在 `_accessible_file_ids` 旁新增：

```python
    def _filter_hits_to_active_files(self, hits: list[dict]) -> list[dict]:
        """Drop hits whose file is missing or soft-deleted (Codex round-3 #3).

        Belt-and-suspenders for the admin/global path where _accessible_file_ids
        returns None (no id filter): a soft-deleted file's vectors may survive until
        the worker physically cleans them, so filter at result time too.
        """
        file_ids = {h.get("file_id") for h in hits if h.get("file_id") is not None}
        if not file_ids:
            return [h for h in hits if h.get("file_id") is not None]
        active = set(
            self.db.execute(
                select(File.id).where(
                    File.id.in_(file_ids),
                    File.deleted_at.is_(None),
                )
            ).scalars().all()
        )
        return [h for h in hits if h.get("file_id") in active]
```

调用点（两处，enrich 之后）：
- `_search_flat`：`self._enrich_hits(hits)` 之后加 `hits = self._filter_hits_to_active_files(hits)`。
- `_search_contextual`：`self._enrich_hits(chunk_hits)` 之后加 `chunk_hits = self._filter_hits_to_active_files(chunk_hits)`。

> `select` 与 `File` 已在 `retrieval_service.py` import（`_accessible_file_ids` 在用）。hits 的 `file_id` 由 `_enrich_hits` / 向量库返回，键名为 `"file_id"`（见 `_search_contextual` 用 `h.get("file_id")`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_retrieval.py -v`
Expected: PASS（3 passed：admin workspace active-only + admin 全删空 + hits 过滤丢弃软删）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/retrieval/retrieval_service.py openrag/tests/test_soft_delete_retrieval.py
git commit -m "feat(retrieval): active-only file ids + _filter_hits_to_active_files drops soft-deleted hits"
```

---

## Task 12: 阶段收尾 — 全量回归

- [ ] **Step 1: 后端全量（含 Phase 1 套件不回归）**

Run:
```
& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_soft_delete_model.py tests/test_task_service_add_task.py tests/test_soft_delete_helpers.py tests/test_soft_delete_files_api.py tests/test_service_api_soft_delete.py tests/test_worker_soft_delete_cleanup.py tests/test_soft_delete_read_paths.py tests/test_soft_delete_write_guard.py tests/test_soft_delete_retrieval.py tests/test_file_tag_model.py tests/test_file_ingest_tag.py tests/test_files_api_tag.py tests/test_service_api_tag.py tests/test_file_ingest_creates_dirs.py tests/test_service_api.py tests/test_workspace_file_tree.py -v
```
Expected: 全 PASS。

必须确认以下端到端语义（覆盖 Codex 审查全部必改项）：
- 删除（内部 `/files/{id}`、外部 `by-path`、`delete-path-prefix`，`background=true`）→ 文档**立即**从 list / by-tag / by-path / tree 消失、**tag 立即可复用**（同 tag 重新上传 201）。
- **回滚**（Codex #4）：`add_task`/`commit` 失败时，内部单文件删除、内部目录/prefix 删除、外部 by-path 删除三处的 `deleted_at`/`tag`/task 三者同事务回滚。
- **prefix 清理不误删**（Codex #1）：前缀删除任务入队后，同 prefix 下新建的 active 行不被 worker 物理删除（worker 只删 `deleted_at <= deleted_before`）；软删目录下后来出现的 active 子项不被目录行级联删除；缺 `deleted_before` 的任务 fail-closed（不物删）。
- **pending-deletion 写入守卫**（Codex #2）：上传到 `/dir`、`create_directory("/dir/new")`、`move_file(... -> "/dir/new.txt")` 在 `/dir` 已软删时均 `409 pending deletion`。
- **软删后不可读**（Codex #3）：`reprocess`、workspace chunks/content/preview/chunk-source、service preview-link、embed preview、search chunk context 均 404；`GET by-tag`/`by-path` 命中软删行 → 404。
- **目录物理清理不删 active 对象**（Codex round-3 #1）：软删 `/dir` 后出现 active `/dir/new.txt`，worker 清理 `/dir` 时**不调用** `remove_directory`（prefix 递归），active 子项对象与 DB 行均保留。
- **根目录删除拒绝**（Codex round-3 #2）：`DELETE /files/{root_id}`（`uri="/"`）→ 400，根下文件 `deleted_at`/`tag`/task 不变；`soft_delete_subtree` 对空/`"/"` prefix 直接 400。
- **检索兜底**（Codex round-3 #3）：admin + `workspace_id=None`、向量命中软删文件时，`_filter_hits_to_active_files` 把软删 hit 过滤掉。
- **语义边界**：软删后同 tag + 不同 URI 可重新上传 201；同 tag + 同 URI 裸上传仍 409（uri 未释放，spec §4.9，等 Phase 3 upsert）。

- [ ] **Step 2: 前端全量（应无改动，仅确认不回归）**

Run: `cd web && npx vitest run && npx tsc --noEmit`
Expected: 全 PASS + 无类型错误。

- [ ] **Step 3: 标记阶段完成**

Phase 2 完成。Phase 3（`PUT upsert-by-tag`：create / update / 同行 move+replace 分支）独立计划。

---

## Self-Review（计划自查）

- **Spec 覆盖**（§4.7/4.9/6/7、§8 Codex #1/#4/#8）：`deleted_at` 列+索引（T1/T2）、非提交 `add_task` + 单事务软删除（T3/T4）、三删除入口统一软删除（T5/T6/T7）、worker 按 `deleted_before` 水位清理（T8）、读路径全审计（T9）、pending-deletion 写守卫（T10）、检索 active-only + admin-empty 修复（T11）、全量回归（T12）。✅ 覆盖。
- **Codex 第二轮审查（2026-06-29）全部采纳**（见下「Codex 审查补充」原文，逐条落入计划）：
  - #1 worker 误删 → T8 缺 `deleted_before` **fail-closed**（不回退 prefix 全删）+ T4 新增 `_physically_delete_row_only`（**不级联 children**），prefix cleanup 逐行删；T4 加「目录软删后出现 active 子项不被级联」回归。
  - #2 写入 pending subtree → **新增 T10** `assert_no_pending_deleted_ancestor`，接入 ingest/`create_directory`/`move_file`，三入口 409 回归。
  - #3 读路径不全 → T9 (e) 补 `reprocess`、`workspace_file_api`（chunks/content/preview/chunk-source）、`resolve_preview_target`、`resolve_claims_file_and_chunk`、`search /chunks/{id}`，并加 reprocess/by-tag/by-path 404 回归。
  - #4 回滚未测 → T5/T6/T7 各加 `add_task` 抛错回滚断言（`deleted_at`/`tag`/task 同回滚），并把目录/prefix 分支收敛到 `soft_delete_subtree_and_enqueue`（单事务+回滚）。
  - #5 时间类型 → `utcnow()` 统一 **naive UTC**（`datetime.now(timezone.utc).replace(tzinfo=None)`），测试同步用 `utcnow()`。
  - #6 测试命令 → 全文改为主仓库 venv 的显式 `& "E:\…\python.exe" -m pytest`。
- **Codex 第三轮审查（2026-06-29，见下「Codex 再次审查补充」）全部采纳**：
  - round-3 #1 目录 row-only 仍会 prefix-递归删对象 → `_physically_delete_row_only` 目录分支**完全不碰 MinIO**（目录是 DB-only 虚拟节点），T4 加「断言 `remove_directory` 未被调用」回归。
  - round-3 #2 删根目录 → `soft_delete_subtree` 对空/`"/"` prefix 400；`DELETE /files/{id}` 显式拒绝 `uri=="/"`；T5 加根目录 400 回归。
  - round-3 #3 检索兜底落地 → 新增 `_filter_hits_to_active_files(hits)`，在 `_search_flat`/`_search_contextual` 的 enrich 后调用；T11 加 hits 过滤回归。
  - round-3 命令一致性 → 文首约定块显式说明**不加** `--import-mode=importlib`（Phase 1 已证默认模式可行），消除与旧审查段的冲突。
- **明确不做（保持 spec 决策）**：软删除**只释放 tag、不释放 uri**——ingest 同 uri 查重**不加** `deleted_at` 过滤（同 uri 裸上传仍 409，T9 (c) 已显式标注「此条不改」）；同 tag+同路径立即重建走 Phase 3 upsert 更新分支。
- **占位符扫描**：每个实现步骤含完整代码/精确锚点，每个测试步骤含完整测试代码与可跑命令。「视实现微调」处已显式标注：T8 worker 测试若 `__new__` 旁路不便则降级为 helper 直测；T11 回查兜底以 `_accessible_file_ids` active-only 为主。
- **类型/签名一致性**：`TaskService.add_task(...)` T3 定义、T4/T5/T6/T7 复用；`_release_tag_and_soft_delete(db, file, *, user_id) -> Task`、`soft_delete_subtree(db, workspace_id, prefix) -> datetime`、`soft_delete_subtree_and_enqueue(db, workspace_id, prefix, *, user_id) -> Task`、`_physically_delete_row_only(db, file, workspace)`、`physically_delete_soft_deleted_under_prefix(db, workspace_id, prefix, deleted_before, workspace) -> list[int]`、`assert_no_pending_deleted_ancestor(db, workspace_id, target_uri)` 定义与调用各处一致；`payload={"path", "deleted_before"}` 写入（T5/T6）与读取（T8）一致。
- **承接 Phase 1**：迁移独立 `20260626_0005`（`down_revision=20260626_0004`）；`GET by-tag` 的 `deleted_at` 过滤在 T9 补齐（Phase 1 已标注遗留）。
- **测试约定**：沿用 Phase 1 实测的 venv/pythonpath/跨文件 fixture 复用/`_root()` 种子；新测试文件用显式路径收集；删除回滚测试用 `pytest.raises`（默认 `TestClient` 会重抛未处理异常）。

---

## Codex 审查补充（2026-06-29）

> 来源：Codex 审查。结论：Phase 2 的方向正确，但执行前建议先修正以下会影响核心语义的风险点；这些不是措辞问题，可能导致软删除后仍可见、误删新文件，或测试无法稳定运行。

### 必须修改

1. **Worker 对 prefix 清理仍可能误删 active 行。**
   - 当前 Task 8 对无 `deleted_before` 的旧任务仍回退到 `delete_files_under_uri_prefix(...)`，这是 prefix 物理全删，和 Phase 2「只物理清理 `deleted_at <= deleted_before` 的软删行」目标冲突。
   - 另外 `physically_delete_soft_deleted_under_prefix(...)` 计划中逐行调用 `delete_file_with_storage(...)`；但 `delete_file_with_storage(...)` 遇到目录行时会递归删除该目录下所有 children，不检查 children 的 `deleted_at`。如果目录已软删、目录下后来又出现 active 子项，清理目录行时仍可能把 active 子项删掉。
   - 建议实现：缺少 `deleted_before` 的 `DELETE_PATH_PREFIX` 任务应 fail closed（返回 skipped/error 并记录日志），或仅调用 soft-deleted-only helper，不允许回退到 prefix-wide delete；同时新增一个“只删除当前行的存储/向量/DB，不递归 children”的物理删除 helper，prefix cleanup 依赖已按深度排序的 rows 逐行删除。

2. **写入口会把 active 行写进 pending-deletion subtree。**
   - 计划已覆盖 `file_ingest.py` 的父目录校验，但还遗漏内部 `create_directory` 和 `move_file`：它们可以在 `/dir` 已软删但 worker 尚未清理时，继续创建或移动 active 行到 `/dir/...`。
   - 建议实现：抽一个最小共享校验 helper，例如 `assert_no_pending_deleted_ancestor(db, workspace_id, target_uri)`；目标路径自身或任一祖先存在 `deleted_at IS NOT NULL` 时返回 `409 pending deletion`。该 helper 至少接入上传/create_dirs、`create_directory`、`move_file` 的目标路径校验。

3. **读路径审计范围还不够，软删后仍可能读到正文/预览/chunk。**
   - Task 9 覆盖了 list/detail/content/tree/by-tag/by-path/retrieval，但实际还有按 `file_id` 直接取文件的入口：`/files/{id}/reprocess`、`workspace_file_api` 的 chunks/content/preview/chunk-source、service preview-link 的 `resolve_preview_target(...)`、embed preview 的 `resolve_claims_file_and_chunk(...)`、search chunk context 的 `/search/chunks/{chunk_id}`。
   - 建议实现：这些入口都应把 `File.deleted_at.is_(None)` 纳入定位查询；已签发的 embed preview token 在文件软删后也应 404。

4. **事务回滚语义写了，但测试没有兜住关键失败路径。**
   - Task 11 写了“任务创建/commit 失败 -> `deleted_at`/`tag` 一起回滚”，但 Task 5/6/7 没有覆盖目录删除、prefix 删除、外部 by-path 删除在 `TaskService.add_task` 或 `db.commit` 失败时的回滚断言。
   - 建议补测试：monkeypatch `TaskService.add_task` 或 `db.commit` 抛错，分别覆盖内部单文件删除、内部目录/prefix 删除、外部 by-path 删除；断言旧文件仍 active、`tag` 未释放、没有 cleanup task 残留。

5. **`deleted_at` 时间类型需要统一。**
   - 计划使用 `TIMESTAMP`，测试/helper 使用 `datetime.now(timezone.utc)`。项目内大多数普通时间列是 naive `DateTime/TIMESTAMP`，PostgreSQL 与 SQLite 在 aware/naive datetime 比较上可能出现差异。
   - 建议实现：Phase 2 统一使用 naive UTC，例如 `datetime.now(timezone.utc).replace(tzinfo=None)`；`deleted_before` payload 也按同一语义序列化/反序列化，避免 `deleted_at <= deleted_before` 跨环境行为不一致。

6. **测试命令约定需要改成真实可执行命令。**
   - worktree 下裸 `python` 会解析到用户目录的 wrapper，可能再次出现 `No module named pytest`；文档中“下文统一写作 `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest ...`”容易误导执行者。
   - 建议把 Phase 2 所有后端测试命令统一写为主仓库 venv 的显式 python，例如：
     ```powershell
     & "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest --import-mode=importlib <files> -v
     ```

### 建议补充回归

- Prefix cleanup：软删 `/dir` 目录和旧 child 后，再出现 active `/dir/new.txt`；worker 清理不得删除 active 行。
- Pending-deletion 写入：`create_directory("/dir/new")`、`move_file(... -> "/dir/new.txt")`、上传到 `/dir` 均返回 `409 pending deletion`。
- 软删后不可读：`reprocess`、workspace chunks/content/preview/chunk-source、service preview-link、embed preview、search chunk context 均返回 404。
- 回滚：task 创建失败或 commit 失败时，`deleted_at`、`tag`、task 三者同事务回滚。
- 语义边界：同 tag + 不同 URI 在删除后可重新上传 201；同 tag + 同 URI 裸上传仍 409，等待 Phase 3 upsert 处理。

---

## Codex 再次审查补充（2026-06-29）

> 来源：Codex 审查。结论：Claude 已采纳上一轮主要意见，Phase 2 方向基本可执行；但执行前仍建议补掉以下几个实质性边界，避免对象存储被误删、根目录被软删，或检索继续返回软删文件。

### 必须修改

1. **目录 row-only 物理删除仍可能删除 active 子项对象。**
   - 当前计划中的 `_physically_delete_row_only()` 虽然不再递归删除 DB children，但对目录行仍调用 `minio_storage.remove_directory(slug, file.uri)`。现有 `remove_directory()` 是按 prefix 递归删除 MinIO 对象，这会把软删目录下后来出现的 active 子项对象一起删掉，造成 DB 行仍 active、对象已丢失。
   - 建议实现：row-only helper 遇到 `file.is_directory` 时不要调用 `remove_directory()`；只清理该目录行自身相关的任务/向量/DB 行。文件对象、文档层级对象由 soft-deleted 文件行逐个清理。若确实需要删除目录 marker，也必须使用“精确 key 删除”而不是 prefix 删除。

2. **删除根目录 `/` 的边界需要显式拒绝。**
   - `soft_delete_subtree_and_enqueue()` 会对 `prefix.rstrip("/")`，如果内部 `DELETE /files/{id}` 删除的是 `uri="/"` 的目录，prefix 会变成空字符串，并可能软删整个 workspace 或写入无效 payload。
   - 建议实现：`DELETE /files/{id}` 对 `file.is_directory and file.uri == "/"` 直接返回 400；`soft_delete_subtree()` / `soft_delete_subtree_and_enqueue()` 内部也防御性拒绝 `"/"` 与空 prefix，保证不会从 helper 误删全 workspace。

3. **检索结果过滤仍需落成具体实现。**
   - Task 11 主要修 `_accessible_file_ids()`，但 admin 且 `workspace_id=None` 时仍返回 `None`，向量/层级索引中尚未物理清理的软删文件仍可能被检索命中。文档目前只写“回查兜底”，但没有明确具体 helper 和调用点。
   - 建议实现：新增一个批量 active 过滤 helper，例如 `_filter_hits_to_active_files(hits)`，从 hits 中取 `file_id` 批量查 `File.id` 且 `File.deleted_at.is_(None)`，只保留 active file 的 hit；`_search_flat()` 在 `_enrich_hits()` 后调用，`_search_contextual()` 至少在 `l0_hits` 得出 `candidate_files` 后和 `chunk_hits` 返回前调用，确保 admin 全局检索也不会返回软删文件。

### 建议补充回归

- 目录 row-only 清理：软删 `/dir` 后新增 active `/dir/new.txt`，worker 清理 `/dir` 时不得调用 prefix 递归删除；断言 active child 的 MinIO object key 未被删除，或用 stub 断言 `remove_directory(slug, "/dir")` 未被调用。
- 根目录删除：`DELETE /files/{root_id}` 返回 400，root 下文件的 `deleted_at`、`tag`、task 均不变。
- 检索兜底：admin、`workspace_id=None`、vector/layer 命中 soft-deleted file 时，结果过滤为空或只保留 active hit。
- 测试命令：正文所有后端 pytest 命令建议统一加 `--import-mode=importlib`；当前文档开头命令没有加，末尾旧 Codex 审查段仍建议加，存在轻微冲突。
