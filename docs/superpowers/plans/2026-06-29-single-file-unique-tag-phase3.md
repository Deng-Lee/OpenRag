# 单文档唯一 tag — Phase 3（`PUT upsert-by-tag` 按 tag 落盘/更新/搬移）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增外部服务端点 `PUT /service/v1/workspaces/{name}/documents/upsert-by-tag`，以 workspace 内唯一 `tag` 为幂等键：tag 不存在→在目标路径创建；tag 已存在且目标路径相同→原地替换内容；tag 已存在但目标路径不同→把该 tag 文档**搬移到新路径并替换内容**。

**Architecture:** 在 `file_ingest.py` 新增编排函数 `upsert_file_by_tag()`，它在一处判定三分支并复用 Phase 1/2 既有能力——create 复用 `ingest_new_file(..., tag=...)`，update 复用 `replace_file_content()`，move+replace 用一个「先清旧 uri 对象/层级 → 改 uri/name → 再 `replace_file_content` 写新 uri」的搬移序列；并复用 Phase 2 的 `assert_no_pending_deleted_ancestor()` 写守卫与 `deleted_at IS NULL` 活跃判定。`service_api.py` 仅加一个薄端点按 outcome 映射 201/200。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy（生产 PostgreSQL、测试 SQLite in-memory）；pytest；无前端改动（与 by-tag/by-path 一样是 service-token M2M 端点）。

**Scope note（承接 Phase 1/2）:**
- Phase 1 已落地：`tag` 列 + `uq_files_workspace_tag`、`_normalize_tag()`、`_violated_unique_constraint()`、`ingest_new_file(..., tag=...)`、`GET by-tag`。**本计划复用，不重复。**
- Phase 2 已落地：`deleted_at` 列、软删除释放 tag（软删行 `tag=NULL`，故 tag 可复用）、`assert_no_pending_deleted_ancestor()` 写守卫、读路径 active 过滤、`replace_file_content()` 仍在 `PUT by-path` 在用。**本计划复用，不重复。**
- 本阶段**无新迁移、无新列**：仅新增一个 service 函数 + 一个端点 + 测试。
- 软删语义不变：`upsert-by-tag` 只认 active（`deleted_at IS NULL`）行；软删行 tag 已是 NULL，自然落入 create 分支。

**测试运行约定（沿用 Phase 1/2 实测）:** worktree 无独立 venv；用主仓库 venv 的显式 python，cwd 设在 `openrag/`：
```powershell
& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest <文件> -v
```
新测试文件用显式路径收集即可。service 端测试跨文件复用 fixture：`from tests.test_service_api import _root, _stub_app_startup, client, db, owner, workspace, service_token_headers, service_token_write_headers`（Phase 1/2 验证可行）。**不加** `--import-mode=importlib`（默认 prepend 模式已实测可行）。

---

## 关键既有接口（实现时直接复用，签名已核对真实源码）

```python
# openrag/src/openrag/services/file_ingest.py
def build_file_uri(path: str, filename: str) -> str: ...          # 拼 parent + filename -> uri
def validate_path(path: str) -> str: ...                          # 归一化 + 拒绝 .. 穿越
def ensure_directory_path(db, workspace, logical_path) -> FileModel: ...   # mkdir -p，独立 commit
def assert_no_pending_deleted_ancestor(db, workspace_id, target_uri) -> None: ...  # Phase 2，命中软删祖先 -> 409
def _normalize_tag(tag) -> Optional[str]: ...                     # 去空白；空 -> None；非法 charset -> 400
def ingest_new_file(db, workspace, owner_user_id, *, parent_logical_path, upload_filename,
                    file_content, content_type, parser_type="auto",
                    require_parent_dir=False, duplicate_status_code=400,
                    tag=None) -> Tuple[FileModel, Optional[Task]]: ...        # Phase 1：建新文件 + 入队
def replace_file_content(db, workspace, acting_user_id, file, *, new_content,
                         content_type, parser_type="auto") -> Optional[Task]: ...  # 原地覆盖 file.uri + 重处理

# openrag/src/openrag/api/files_api.py
def cleanup_file_processing_data(file, workspace_slug, db) -> None: ...   # 删 hierarchy(按 file.uri)/chunks(按 file_id)/milvus，重置状态

# openrag/src/openrag/storage/minio_storage.py  (MinioStorage 实例方法)
def remove_file(self, bucket_name, object_name): ...
def remove_document_hierarchy(self, bucket_name, file_uri): ...

# openrag/src/openrag/api/service_api.py
def _upload_response_dict(file_record, task_id) -> dict: ...      # 返回 id/path/name/tag/.../task_id
require_workspace_for_name(db, workspace_name) -> Workspace
assert_token_workspace_permission(ctx, ws_id, "write") -> None
```

**已核对的关键事实：**
- `ingest_new_file` 的 tag 查重为 `FileModel.tag == normalized_tag`（无 `deleted_at` 过滤）；软删行 `tag=NULL` 不会命中，故 tag 释放后可复用。**create 分支天然正确。**
- `replace_file_content` 用 `file.uri` 作为 MinIO put 目标，并先 `cleanup_file_processing_data(file)`（hierarchy 按 `file.uri`、chunks 按 `file_id`）。**move 分支必须先在旧 uri 清理 + 删旧源对象，再改 `file.uri`，最后调 `replace_file_content` 写新 uri。**
- `move_file`（files_api）改 uri/name 时**不更新 `parent_id`**（uri 为路径真相源）。本计划 move 分支沿用此先例：只改 uri/name，不动 parent_id；目录树由 uri 推导。
- `uq_files_workspace_uri` 保证同 workspace 同 uri 唯一；create 分支撞 uri 由 `ingest_new_file` 现有 IntegrityError 分类成 409。

---

## 文件结构（本阶段创建/修改）

| 文件 | 职责 |
|---|---|
| `openrag/src/openrag/services/file_ingest.py`（改） | 新增 `upsert_file_by_tag()` 编排（三分支：create / update-in-place / move+replace），返回 `(FileModel, Optional[Task], outcome)` |
| `openrag/src/openrag/api/service_api.py`（改） | 新增 `PUT .../documents/upsert-by-tag` 端点，按 outcome 映射 `201`(created) / `200`(updated|moved_replaced) |
| `openrag/tests/test_file_ingest_upsert.py`（建） | `upsert_file_by_tag` 三分支 + 边界的服务层单测（SQLite，stub MinIO） |
| `openrag/tests/test_service_api_upsert.py`（建） | 端点级 E2E（service token，状态码 + 响应体 + tag/路径语义） |

---

## Task 1: `upsert_file_by_tag` — create 分支（tag 不存在）

**Files:**
- Modify: `openrag/src/openrag/services/file_ingest.py`（文件末尾新增函数）
- Test: `openrag/tests/test_file_ingest_upsert.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_file_ingest_upsert.py`:

```python
"""upsert_file_by_tag: create / update-in-place / move+replace branches (service layer)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, Task, User, Workspace
from openrag.services import file_ingest
from openrag.services.file_ingest import upsert_file_by_tag


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


@pytest.fixture()
def stub_minio(monkeypatch):
    """Stub MinIO so service-layer tests never touch object storage."""
    class _M:
        def put_file(self, *a, **k): return None
        def remove_file(self, *a, **k): return None
        def remove_document_hierarchy(self, *a, **k): return None
    # patch every module that constructs MinioStorage on the upsert path
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr("openrag.api.files_api.MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr("openrag.api.files_api.delete_milvus_vectors_for_file", lambda *a, **k: [])
    return _M()


def test_upsert_creates_when_tag_absent(db, wsowner, stub_minio):
    w, u = wsowner
    f, task, outcome = upsert_file_by_tag(
        db, w, u.id,
        tag="report",
        parent_logical_path="/",
        upload_filename="r.txt",
        file_content=b"hello",
        content_type="text/plain",
        parser_type="txt",
        create_dirs=True,
    )
    assert outcome == "created"
    assert f.tag == "report"
    assert f.uri == "/r.txt"
    assert f.deleted_at is None
    assert task is not None  # process_document enqueued
    assert db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).count() == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: FAIL — `ImportError: cannot import name 'upsert_file_by_tag'`。

- [ ] **Step 3: 实现 create 分支**

In `openrag/src/openrag/services/file_ingest.py`，文件末尾（`replace_file_content` 之后）新增：

```python
def _active_tagged_file(db: Session, workspace_id: int, tag: str) -> Optional[FileModel]:
    """Return the single ACTIVE (deleted_at IS NULL) non-directory row carrying ``tag``."""
    return (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            FileModel.tag == tag,
            FileModel.is_directory.is_(False),
            FileModel.deleted_at.is_(None),
        )
        .first()
    )


def upsert_file_by_tag(
    db: Session,
    workspace: Workspace,
    owner_user_id: int,
    *,
    tag: str,
    parent_logical_path: str,
    upload_filename: str,
    file_content: bytes,
    content_type: Optional[str],
    parser_type: str = "auto",
    create_dirs: bool = False,
) -> Tuple[FileModel, Optional[Task], str]:
    """Idempotent upsert keyed by per-workspace ``tag``.

    Branches on the single ACTIVE row carrying ``tag``:
    - none                       -> CREATE at parent/filename (outcome "created")
    - exists, same target uri    -> UPDATE content in place   (outcome "updated")
    - exists, different uri      -> MOVE the row to the new uri + replace content
                                    (outcome "moved_replaced")

    Returns ``(file, task, outcome)``. Raises HTTPException(400) on an invalid/empty tag.
    """
    normalized_tag = _normalize_tag(tag)
    if normalized_tag is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upsert-by-tag requires a non-empty tag",
        )

    target_uri = build_file_uri(validate_path(parent_logical_path), upload_filename)
    existing = _active_tagged_file(db, workspace.id, normalized_tag)

    if existing is None:
        file_record, task = ingest_new_file(
            db,
            workspace,
            owner_user_id,
            parent_logical_path=parent_logical_path,
            upload_filename=upload_filename,
            file_content=file_content,
            content_type=content_type,
            parser_type=parser_type,
            require_parent_dir=not create_dirs,
            duplicate_status_code=status.HTTP_409_CONFLICT,
            tag=normalized_tag,
        )
        return file_record, task, "created"

    raise NotImplementedError  # update / move branches added in Task 2 / Task 3
```

> `_normalize_tag`、`build_file_uri`、`validate_path`、`ingest_new_file`、`FileModel`、`Task`、`Optional`、`Tuple`、`HTTPException`、`status`、`Workspace`、`Session` 均已在 `file_ingest.py` import（现有函数在用）。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: PASS（1 passed）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/tests/test_file_ingest_upsert.py
git commit -m "feat(upsert): upsert_file_by_tag create branch (tag absent -> ingest with tag)"
```

---

## Task 2: update-in-place 分支（tag 存在且目标路径相同）

**Files:**
- Modify: `openrag/src/openrag/services/file_ingest.py`（`upsert_file_by_tag` 内替换 `raise NotImplementedError`）
- Test: `openrag/tests/test_file_ingest_upsert.py`（追加）

- [ ] **Step 1: 追加失败测试**

Append to `openrag/tests/test_file_ingest_upsert.py`:

```python
def test_upsert_updates_in_place_when_tag_and_path_match(db, wsowner, stub_minio):
    w, u = wsowner
    # seed an existing tagged file at /r.txt
    f0, _, oc0 = upsert_file_by_tag(
        db, w, u.id, tag="report", parent_logical_path="/", upload_filename="r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert oc0 == "created"
    original_id = f0.id

    f1, task, outcome = upsert_file_by_tag(
        db, w, u.id, tag="report", parent_logical_path="/", upload_filename="r.txt",
        file_content=b"v2-longer", content_type="text/plain", parser_type="txt",
    )
    assert outcome == "updated"
    assert f1.id == original_id          # same row reused
    assert f1.uri == "/r.txt"
    assert f1.tag == "report"
    assert f1.size == len(b"v2-longer")  # content metadata refreshed
    assert task is not None
    # still exactly one active row with this tag
    assert db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).count() == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py::test_upsert_updates_in_place_when_tag_and_path_match -v`
Expected: FAIL — `NotImplementedError`（命中 existing 但分支未实现）。

- [ ] **Step 3: 实现 update-in-place 分支**

In `openrag/src/openrag/services/file_ingest.py`，把 `upsert_file_by_tag` 末尾的 `raise NotImplementedError` 替换为：

```python
    if existing.uri == target_uri:
        task = replace_file_content(
            db,
            workspace,
            owner_user_id,
            existing,
            new_content=file_content,
            content_type=content_type,
            parser_type=parser_type,
        )
        db.refresh(existing)
        return existing, task, "updated"

    raise NotImplementedError  # move+replace branch added in Task 3
```

> `replace_file_content` 已在同文件定义（无需 import），内部完成大小/类型校验、`cleanup_file_processing_data`、MinIO 覆盖写、`process_document` 入队。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/tests/test_file_ingest_upsert.py
git commit -m "feat(upsert): update-in-place branch (tag + same uri -> replace_file_content)"
```

---

## Task 3: move+replace 分支（tag 存在但目标路径不同）

**Files:**
- Modify: `openrag/src/openrag/services/file_ingest.py`（`upsert_file_by_tag` 内替换第二个 `raise NotImplementedError`；新增搬移 helper）
- Test: `openrag/tests/test_file_ingest_upsert.py`（追加）

- [ ] **Step 1: 追加失败测试**

Append to `openrag/tests/test_file_ingest_upsert.py`:

```python
def test_upsert_moves_and_replaces_when_path_differs(db, wsowner, stub_minio):
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", parent_logical_path="/", upload_filename="r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    original_id = f0.id

    f1, task, outcome = upsert_file_by_tag(
        db, w, u.id, tag="report", parent_logical_path="/archive", upload_filename="r2.txt",
        file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert outcome == "moved_replaced"
    assert f1.id == original_id          # SAME row relocated, not a new one
    assert f1.uri == "/archive/r2.txt"
    assert f1.name == "r2.txt"
    assert f1.tag == "report"
    assert task is not None
    # exactly one active row with the tag; old uri no longer has an active row
    assert db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).count() == 1
    assert db.query(File).filter(File.uri == "/r.txt", File.deleted_at.is_(None)).count() == 0


def test_upsert_move_rejects_when_target_uri_occupied_by_other_file(db, wsowner, stub_minio):
    w, u = wsowner
    # tagged doc at /r.txt
    upsert_file_by_tag(
        db, w, u.id, tag="report", parent_logical_path="/", upload_filename="r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # an unrelated active file already sits at /archive/r2.txt (no tag)
    other = File(uri="/archive/r2.txt", name="r2.txt", owner_id=u.id, workspace_id=w.id,
                 is_directory=False, size=1)
    db.add(other); db.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="report", parent_logical_path="/archive", upload_filename="r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 409


def test_upsert_move_rejects_pending_deleted_ancestor(db, wsowner, stub_minio):
    """Phase 2 write-guard: cannot relocate a tagged doc into a soft-deleted subtree."""
    w, u = wsowner
    upsert_file_by_tag(
        db, w, u.id, tag="report", parent_logical_path="/", upload_filename="r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # /archive directory exists but is soft-deleted (pending cleanup)
    from openrag.services.file_deletion import utcnow
    arch = File(uri="/archive", name="archive", owner_id=u.id, workspace_id=w.id,
                is_directory=True, size=0, deleted_at=utcnow())
    db.add(arch); db.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="report", parent_logical_path="/archive", upload_filename="r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: FAIL — `NotImplementedError`（move 分支未实现）。

- [ ] **Step 3: 实现 move+replace 分支**

In `openrag/src/openrag/services/file_ingest.py`，在 `upsert_file_by_tag` 之前新增搬移 helper：

```python
def _relocate_active_file(
    db: Session, workspace: Workspace, file: FileModel, target_uri: str, *, create_dirs: bool
) -> None:
    """Repoint an ACTIVE file row from ``file.uri`` to ``target_uri`` (no content yet).

    Mirrors files_api.move_file precedent: uri/name are the path source of truth
    (parent_id is left untouched). Refuses to land in a pending-deletion subtree
    (Phase 2 write-guard) or on top of another active row, and removes the old uri's
    source object + derived hierarchy so the subsequent replace_file_content writes a
    clean new uri. Caller does replace_file_content afterward (reprocess + new bytes).
    """
    assert_no_pending_deleted_ancestor(db, workspace.id, target_uri)

    occupied = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace.id,
            FileModel.uri == target_uri,
            FileModel.deleted_at.is_(None),
        )
        .first()
    )
    if occupied is not None and occupied.id != file.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target path already occupied by another document: {target_uri}",
        )

    if create_dirs:
        parent = posixpath.dirname(target_uri) or "/"
        ensure_directory_path(db, workspace, parent)

    old_uri = file.uri
    minio_storage = MinioStorage()
    try:
        minio_storage.remove_file(workspace.slug, old_uri)
        minio_storage.remove_document_hierarchy(workspace.slug, old_uri)
    except Exception:  # noqa: BLE001 - object cleanup is best-effort; row move must proceed
        pass

    file.uri = target_uri
    file.name = posixpath.basename(target_uri)
    db.add(file)
    db.commit()
    db.refresh(file)
```

然后把 `upsert_file_by_tag` 末尾第二个 `raise NotImplementedError` 替换为：

```python
    # existing.uri != target_uri -> relocate the tagged row, then replace its content.
    _relocate_active_file(db, workspace, existing, target_uri, create_dirs=create_dirs)
    task = replace_file_content(
        db,
        workspace,
        owner_user_id,
        existing,
        new_content=file_content,
        content_type=content_type,
        parser_type=parser_type,
    )
    db.refresh(existing)
    return existing, task, "moved_replaced"
```

> `assert_no_pending_deleted_ancestor`、`ensure_directory_path`、`MinioStorage`、`posixpath` 均已在 `file_ingest.py` import（现有函数在用）。`replace_file_content` 复用：搬移后 `file.uri` 已是 target，`cleanup_file_processing_data` 按 `file_id` 删 chunks/milvus、按新 uri 删 hierarchy（新 uri 暂无 hierarchy，no-op），再 `put_file` 写新 uri 并入队。旧 uri 的源对象 + 旧 hierarchy 已在 `_relocate_active_file` 删除。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: PASS（5 passed：create + update + move + 目标占用 409 + pending 祖先 409）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/tests/test_file_ingest_upsert.py
git commit -m "feat(upsert): move+replace branch (tag + different uri -> relocate row + replace)"
```

---

## Task 4: service 端点 `PUT .../documents/upsert-by-tag`

**Files:**
- Modify: `openrag/src/openrag/api/service_api.py`（`service_replace_document` 之后新增路由）
- Test: `openrag/tests/test_service_api_upsert.py`

- [ ] **Step 1: 写失败测试**

Create `openrag/tests/test_service_api_upsert.py`:

```python
"""External PUT upsert-by-tag: 201 create / 200 update / 200 move+replace, by tag."""

import io
import pytest

from openrag.models import File, Task

from tests.test_service_api import (  # noqa: F401
    _root, _stub_app_startup, client, db, owner, workspace,
    service_token_headers, service_token_write_headers,
)


def _put_upsert(client, workspace, headers, *, tag, path, filename, content, parser="txt"):
    return client.put(
        f"/service/v1/workspaces/{workspace.name}/documents/upsert-by-tag",
        params={"tag": tag},
        data={"path": path, "parser_type": parser, "create_dirs": "true"},
        files={"file": (filename, io.BytesIO(content), "text/plain")},
        headers=headers,
    )


def test_upsert_creates_returns_201(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", path="/", filename="a.txt", content=b"hello")
    assert r.status_code == 201
    body = r.json()
    assert body["tag"] == "t1" and body["path"] == "/a.txt"
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1


def test_upsert_update_in_place_returns_200(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    _put_upsert(client, workspace, service_token_write_headers,
                tag="t1", path="/", filename="a.txt", content=b"v1")
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", path="/", filename="a.txt", content=b"v2-longer")
    assert r.status_code == 200
    assert r.json()["path"] == "/a.txt"
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1


def test_upsert_move_and_replace_returns_200(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    _put_upsert(client, workspace, service_token_write_headers,
                tag="t1", path="/", filename="a.txt", content=b"v1")
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", path="/archive", filename="b.txt", content=b"v2")
    assert r.status_code == 200
    assert r.json()["path"] == "/archive/b.txt"
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1
    assert db.query(File).filter(File.uri == "/a.txt", File.deleted_at.is_(None)).count() == 0


def test_upsert_requires_write_token(client, db, workspace, owner, service_token_headers):
    """read-only token cannot upsert."""
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_headers,
                    tag="t1", path="/", filename="a.txt", content=b"v1")
    assert r.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_service_api_upsert.py -v`
Expected: FAIL — 路由不存在（404 / 405）。

- [ ] **Step 3: 实现端点**

In `openrag/src/openrag/api/service_api.py`，在 `service_replace_document`（`PUT by-path`）之后新增：

```python
@router.put("/workspaces/{workspace_name}/documents/upsert-by-tag")
async def service_upsert_document_by_tag(
    workspace_name: str,
    tag: str = Query(..., min_length=1, description="Per-workspace unique tag (idempotency key)"),
    path: str = Form(..., description="Parent directory logical path for the document"),
    file: UploadFile = File(...),
    parser_type: str = Form(default="auto"),
    create_dirs: bool = Form(
        default=False,
        description="Create missing parent directories (mkdir -p) before upsert",
    ),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Idempotent-by-tag upsert: create (201) / update-in-place (200) / move+replace (200)."""
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    body = await file.read()
    file_record, task_record, outcome = upsert_file_by_tag(
        db,
        ws,
        ws.owner_id,
        tag=tag,
        parent_logical_path=path,
        upload_filename=file.filename or "unnamed",
        file_content=body,
        content_type=file.content_type,
        parser_type=parser_type,
        create_dirs=create_dirs,
    )
    status_code = status.HTTP_201_CREATED if outcome == "created" else status.HTTP_200_OK
    payload = _upload_response_dict(file_record, task_record.id if task_record else None)
    payload["outcome"] = outcome
    return JSONResponse(status_code=status_code, content=payload)
```

并确认 `service_api.py` 顶部已 import `upsert_file_by_tag`：把现有 `from openrag.services.file_ingest import (...)` 块追加 `upsert_file_by_tag`（与 `ingest_new_file`、`replace_file_content`、`validate_path` 并列）。`Query`、`Form`、`File`、`UploadFile`、`JSONResponse`、`status` 均已在用。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_service_api_upsert.py -v`
Expected: PASS（4 passed：create 201 / update 200 / move 200 / read-token 403）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/service_api.py openrag/tests/test_service_api_upsert.py
git commit -m "feat(service): PUT documents/upsert-by-tag (201 create / 200 update|move) keyed by tag"
```

---

## Task 5: 边界 — 非法 tag / 软删 tag 复用 / 目录目标 / 大小校验继承

**Files:**
- Test: `openrag/tests/test_file_ingest_upsert.py`（追加；本任务多为验证既有行为，按需补实现）

- [ ] **Step 1: 追加失败/验证测试**

Append to `openrag/tests/test_file_ingest_upsert.py`:

```python
def test_upsert_empty_tag_rejected_400(db, wsowner, stub_minio):
    w, u = wsowner
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="   ", parent_logical_path="/", upload_filename="a.txt",
            file_content=b"x", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 400


def test_upsert_invalid_tag_charset_rejected_400(db, wsowner, stub_minio):
    w, u = wsowner
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="bad/tag", parent_logical_path="/", upload_filename="a.txt",
            file_content=b"x", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 400


def test_upsert_treats_soft_deleted_tag_as_create(db, wsowner, stub_minio):
    """A soft-deleted row has tag=NULL, so its old tag is free -> upsert creates anew."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="reuse", parent_logical_path="/", upload_filename="a.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    from openrag.services.file_deletion import utcnow
    f0.deleted_at = utcnow(); f0.tag = None  # simulate Phase 2 soft delete
    db.commit()

    f1, _, outcome = upsert_file_by_tag(
        db, w, u.id, tag="reuse", parent_logical_path="/", upload_filename="b.txt",
        file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert outcome == "created"
    assert f1.id != f0.id and f1.tag == "reuse" and f1.uri == "/b.txt"
    assert db.query(File).filter(File.tag == "reuse", File.deleted_at.is_(None)).count() == 1
```

- [ ] **Step 2: 跑测试确认结果**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: 全 PASS（8 passed）。空/非法 tag 由 `_normalize_tag` + upsert 入口 400；软删 tag 因 `tag=NULL` 落入 create——**这些行为已由 Task 1–3 的实现覆盖，本任务仅加回归确认，无需改实现**。若 `test_upsert_empty_tag_rejected_400` 意外失败，核对 `upsert_file_by_tag` 入口的 `if normalized_tag is None: raise HTTPException(400, ...)`。

- [ ] **Step 3: 提交**

```bash
git add openrag/tests/test_file_ingest_upsert.py
git commit -m "test(upsert): boundary regressions (invalid tag 400, soft-deleted tag -> create)"
```

---

## Task 6: 阶段收尾 — 全量回归

- [ ] **Step 1: 后端全量（Phase 1/2/3 套件不回归）**

Run:
```
& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py tests/test_service_api_upsert.py tests/test_file_ingest_tag.py tests/test_files_api_tag.py tests/test_service_api_tag.py tests/test_file_ingest_creates_dirs.py tests/test_service_api.py tests/test_soft_delete_helpers.py tests/test_soft_delete_files_api.py tests/test_service_api_soft_delete.py -v
```
Expected: 全 PASS。

必须确认以下端到端语义：
- **create**：tag 不存在 → `201`，新行带 tag、落在 parent/filename、入队 `process_document`。
- **update-in-place**：tag 存在且 parent/filename 拼出的 uri == 现有 uri → `200`，**同一行**内容替换、size/mime 刷新、重新入队；tag/uri 不变。
- **move+replace**：tag 存在但目标 uri 不同 → `200`，**同一行**搬到新 uri（name 随之改）、内容替换、旧 uri 不再有 active 行、tag 跟随；旧 uri 源对象与旧 hierarchy 已清。
- **幂等键唯一性**：任一分支后，`(workspace, tag, deleted_at IS NULL)` 始终恰好 1 行。
- **冲突**：目标 uri 被另一 active 文件占用 → `409`；搬入软删子树（pending 祖先）→ `409`；create 撞 uri → `409`（`ingest_new_file` 既有分类）。
- **校验继承**：空/非法 tag → `400`；超 `MAX_FILE_SIZE` → `413`；非法 `parser_type`/不支持 MIME → `400`（由 `ingest_new_file`/`replace_file_content` 既有校验提供）。
- **权限**：read-only service token → `403`。
- **软删 tag 复用**：tag 被软删释放后，upsert 走 create 分支重新铸造。

- [ ] **Step 2: 标记阶段完成**

Phase 3 完成。三阶段（Phase 1 tag 唯一性 / Phase 2 软删除 / Phase 3 upsert-by-tag）合起来构成「单文档唯一 tag」的完整生命周期：建→查→改/搬→删→tag 复用。

- [ ] **Step 3: 收尾**

调用 superpowers:finishing-a-development-branch 决定整分支去向（合并 / PR / 保留 / 丢弃）。

---

## Self-Review（计划自查）

- **Spec 覆盖**：三分支（create / update-in-place / move+replace）分别由 Task 1/2/3 实现；端点 + 状态码映射 Task 4；边界（tag 校验、软删复用）Task 5；全量回归 Task 6。✅
- **复用而非重造（DRY/YAGNI）**：create 复用 `ingest_new_file`、update 复用 `replace_file_content`、move 用最小搬移 helper + 复用 `replace_file_content`；写守卫复用 Phase 2 `assert_no_pending_deleted_ancestor`；无新列/无新迁移。✅
- **占位符扫描**：每个实现步骤含完整代码与精确锚点；每个测试步骤含完整测试代码与可跑命令；Task 5 显式标注「多为验证既有行为、按需改实现」。无 TBD/TODO。✅
- **类型/签名一致性**：`upsert_file_by_tag(db, workspace, owner_user_id, *, tag, parent_logical_path, upload_filename, file_content, content_type, parser_type="auto", create_dirs=False) -> Tuple[FileModel, Optional[Task], str]` 在 Task 1 定义、Task 2/3 续写、Task 4 端点调用三处一致；`outcome ∈ {"created","updated","moved_replaced"}` 与端点 201/200 映射一致；`_relocate_active_file`、`_active_tagged_file` 签名与调用一致。✅
- **承接 Phase 1/2**：tag 查重依赖软删行 `tag=NULL`（Phase 2 保证）；`deleted_at IS NULL` 活跃判定贯穿；`replace_file_content` 仍是 `PUT by-path` 与本端点共用。✅
- **已知边界/精确点**：move 分支沿用 `move_file` 先例不改 `parent_id`（uri 为真相源）；`cleanup_file_processing_data` 按 `file.uri` 删 hierarchy，故搬移**先清旧 uri 再改 uri**；MinIO 旧源对象显式 `remove_file` 删除（`replace_file_content` 不删旧 uri 源对象）。
- **测试约定**：沿用 Phase 1/2 实测的 venv/pythonpath/跨文件 fixture 复用/`_root()` 种子；服务层单测 stub MinIO（`file_ingest.MinioStorage` 与 `files_api.MinioStorage`/`delete_milvus_vectors_for_file` 都要 patch，因 `replace_file_content` 经 `cleanup_file_processing_data` 触达 files_api 的 MinIO）。

---

## 待执行时复核的小风险（执行者注意）

- **`replace_file_content` 的内部 commit 边界**：它自行 commit 两次（cleanup 后、写入后）。move 分支里 `_relocate_active_file` 也 commit 一次（改 uri）。三处 commit 不在同一事务——若中途失败可能留下「行已搬、内容未写」的中间态。本阶段沿用既有 `PUT by-path` 的同款非原子性（既有 replace 也非原子）；如需强一致可在后续单列任务收敛，**不在本计划扩范围**。
- **service 层单测 stub 完整性**：`replace_file_content` → `cleanup_file_processing_data`（files_api）会 new `MinioStorage()` 并调 `delete_milvus_vectors_for_file`；测试 fixture `stub_minio` 已 patch 这两处。若执行时报真实 MinIO/Milvus 连接错，核对 patch 目标模块路径。
