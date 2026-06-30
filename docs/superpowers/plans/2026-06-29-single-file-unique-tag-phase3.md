# 单文档唯一 tag — Phase 3（`PUT upsert-by-tag` 按 tag 落盘/更新/搬移）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增外部服务端点 `PUT /service/v1/workspaces/{name}/documents/upsert-by-tag`，以 workspace 内唯一 `tag` 为幂等键：tag 不存在→在目标路径创建；tag 已存在且目标路径相同→原地替换内容；tag 已存在但目标路径不同→把该 tag 文档**搬移到新路径并替换内容**。

**Architecture:** 在 `file_ingest.py` 新增编排函数 `upsert_file_by_tag()`，以**显式 `target_path`（完整文件路径，spec §4.8/§8.5#10）**为输入，在一处判定三分支：create 复用 `ingest_new_file(..., tag=...)`（由 `target_path` 拆 parent/basename）；update（`existing.uri == target_uri`）复用 `replace_file_content()`；move+replace（异 uri）**不串联** `replace_file_content`/`move_file`（二者多次 commit、失败窗口大），而用专用 helper `_move_replace_no_intermediate_commit()`——先校验、先写新对象到 `target_uri`，再在**单次 DB 事务**内更新同一行 `uri/name/size/mime_type/parser_type/processing` 字段 + 删旧 `DocumentChunk` + 非提交 `add_task(process_document)`，单次 `commit`；commit 失败 `rollback` + best-effort 删刚写的新对象；commit 成功后 best-effort 清旧 `old_uri` 对象/层级/向量（绝不删行、绝不释放 tag → 失败时原 tag 与原文档至少一者可达）。复用 Phase 2 `assert_no_pending_deleted_ancestor()` 写守卫与 `deleted_at IS NULL` 活跃判定。`service_api.py` 加一个薄端点，按 **`action`**（`created`/`updated`/`moved`，spec §4.8）映射 `201`/`200`。

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
def assert_no_pending_deleted_ancestor(db, workspace_id, target_uri) -> None: ...  # Phase 2，命中软删祖先（含 target_uri 自身）-> 409
def _assert_parent_directory_exists(db, workspace_id, parent_logical_path) -> None: ...  # 父目录非 active -> 404/400（service upload 在 create_dirs=False 时用）
def _normalize_tag(tag) -> Optional[str]: ...                     # 去空白；空 -> None；非法 charset -> 400
def resolve_effective_mime_type(content_type, filename, parser_type=None) -> str: ...  # 推断有效 MIME
def ingest_new_file(db, workspace, owner_user_id, *, parent_logical_path, upload_filename,
                    file_content, content_type, parser_type="auto",
                    require_parent_dir=False, duplicate_status_code=400,
                    tag=None) -> Tuple[FileModel, Optional[Task]]: ...        # Phase 1：建新文件 + 入队
def replace_file_content(db, workspace, acting_user_id, file, *, new_content,
                         content_type, parser_type="auto") -> Optional[Task]: ...  # 原地覆盖 file.uri + 重处理（多次 commit）
# 模块内常量：SUPPORTED_PARSER_TYPES、MAX_FILE_SIZE、ALLOWED_MIME_TYPES（move helper 校验复用）

# openrag/src/openrag/services/task_service.py
class TaskService:
    def add_task(self, *, workspace_id, user_id, file_id=None, task_type="process_document",
                 queue="normal", priority=5, max_retries=3, status=None, payload=None) -> Task: ...  # Phase 2：非提交，供单事务复用

# openrag/src/openrag/api/files_api.py
def cleanup_file_processing_data(file, workspace_slug, db) -> None: ...   # 删 hierarchy(按 file.uri)/chunks(按 file_id)/milvus，重置状态（update 分支经 replace_file_content 间接用）

# openrag/src/openrag/services/file_deletion.py
def delete_milvus_vectors_for_file(file_id) -> list: ...          # 按 file_id 删向量（move helper 提交后 best-effort 清旧向量）

# openrag/src/openrag/models/document_chunk.py / models/file.py
DocumentChunk            # move helper 单事务内 delete(file_id=...)
ProcessingStatus.pending # move helper 单事务内重置 processing_status

# openrag/src/openrag/storage/minio_storage.py  (MinioStorage 实例方法)
def put_file(self, bucket_name, object_name, data, content_type=None): ...
def remove_file(self, bucket_name, object_name): ...
def remove_document_hierarchy(self, bucket_name, file_uri): ...

# openrag/src/openrag/api/service_api.py
def _upload_response_dict(file_record, task_id) -> dict: ...      # 返回 id/path/name/tag/.../task_id
require_workspace_for_name(db, workspace_name) -> Workspace
assert_token_workspace_permission(ctx, ws_id, "write") -> None
```

**已核对的关键事实：**
- `ingest_new_file` 的 tag 查重为 `FileModel.tag == normalized_tag`（无 `deleted_at` 过滤）；软删行 `tag=NULL` 不会命中，故 tag 释放后可复用。**create 分支天然正确。**
- **`target_path` 是完整文件路径**（spec §4.8/§8.5#10）；upsert 入口 `target_uri = validate_path(target_path)`，**不**由 multipart `file.filename` 派生 uri（避免「改了上传文件名→误触发 move」）。create 分支由 `target_uri` 拆 `parent = posixpath.dirname(target_uri) or "/"`、`basename = posixpath.basename(target_uri)`。
- **move 分支不串联 `replace_file_content`/`move_file`**（spec §4.8 L151-157）：二者各自多次 commit / 带存储副作用，无法满足「失败时原 tag 与原文档可达性不丢」。改用专用 `_move_replace_no_intermediate_commit()`：先写新对象 → 单事务改行+删 chunk+`add_task` → 单 commit → 失败回滚并删新对象 → 成功后才清旧对象/层级/向量。**绝不删行、绝不释放 tag。**
- `move_file`（files_api）改 uri/name 时**不更新 `parent_id`**（uri 为路径真相源）。本计划 move 分支沿用此先例：只改 uri/name，不动 parent_id；目录树由 uri 推导。
- `uq_files_workspace_uri` 保证同 workspace 同 uri 唯一；create 分支撞 uri 由 `ingest_new_file` 现有 IntegrityError 分类成 409。`assert_no_pending_deleted_ancestor` 把 `target_uri` 自身纳入 candidates，故软删目标 uri 已能命中 pending deletion 409，无需额外逻辑。

---

## 文件结构（本阶段创建/修改）

| 文件 | 职责 |
|---|---|
| `openrag/src/openrag/services/file_ingest.py`（改） | 新增 `upsert_file_by_tag()` 编排（以 `target_path` 为输入；三分支 create/update/move）+ 专用 `_move_replace_no_intermediate_commit()`（单事务、失败不丢原文档）+ `_active_tagged_file()`；返回 `(FileModel, Optional[Task], action)`，`action ∈ {"created","updated","moved"}`。新增 import：`ProcessingStatus`、`DocumentChunk`、`delete_milvus_vectors_for_file` |
| `openrag/src/openrag/api/service_api.py`（改） | 新增 `PUT .../documents/upsert-by-tag` 端点：Form `tag` + `target_path` + `parser_type` + `create_dirs`；按 `action` 映射 `201`(created) / `200`(updated\|moved)；响应体含 `action` |
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
    # patch every module that constructs MinioStorage or deletes vectors on the upsert path:
    # - file_ingest.MinioStorage: move helper put/remove + create via ingest_new_file
    # - file_ingest.delete_milvus_vectors_for_file: move helper post-commit old-vector cleanup
    # - files_api.MinioStorage / files_api.delete_milvus_vectors_for_file: update branch goes
    #   through replace_file_content -> cleanup_file_processing_data (defined in files_api)
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr("openrag.services.file_ingest.delete_milvus_vectors_for_file", lambda *a, **k: [])
    monkeypatch.setattr("openrag.api.files_api.MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr("openrag.api.files_api.delete_milvus_vectors_for_file", lambda *a, **k: [])
    return _M()


def test_upsert_creates_when_tag_absent(db, wsowner, stub_minio):
    w, u = wsowner
    f, task, action = upsert_file_by_tag(
        db, w, u.id,
        tag="report",
        target_path="/r.txt",
        file_content=b"hello",
        content_type="text/plain",
        parser_type="txt",
        create_dirs=True,
    )
    assert action == "created"
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
    target_path: str,
    file_content: bytes,
    content_type: Optional[str],
    parser_type: str = "auto",
    create_dirs: bool = False,
) -> Tuple[FileModel, Optional[Task], str]:
    """Idempotent upsert keyed by per-workspace ``tag`` (spec §4.8).

    ``target_path`` is the FULL file logical path (e.g. ``/dir/name.pdf``); the uri is
    NEVER derived from the multipart filename. Branches on the single ACTIVE row carrying
    ``tag``:
    - none                       -> CREATE at target_path           (action "created")
    - exists, existing.uri == target_uri -> UPDATE content in place (action "updated")
    - exists, existing.uri != target_uri -> same-row MOVE + replace  (action "moved")

    Returns ``(file, task, action)``. Raises HTTPException(400) on an invalid/empty tag.
    """
    normalized_tag = _normalize_tag(tag)
    if normalized_tag is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upsert-by-tag requires a non-empty tag",
        )

    target_uri = validate_path(target_path)
    existing = _active_tagged_file(db, workspace.id, normalized_tag)

    if existing is None:
        file_record, task = ingest_new_file(
            db,
            workspace,
            owner_user_id,
            parent_logical_path=posixpath.dirname(target_uri) or "/",
            upload_filename=posixpath.basename(target_uri),
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

> `_normalize_tag`、`validate_path`、`ingest_new_file`、`posixpath`、`FileModel`、`Task`、`Optional`、`Tuple`、`HTTPException`、`status`、`Workspace`、`Session` 均已在 `file_ingest.py` import（现有函数在用）。`target_path` 是完整文件路径，create 分支以其 `dirname`/`basename` 喂 `ingest_new_file`，**不再用 multipart 文件名决定 uri**。

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
    f0, _, a0 = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert a0 == "created"
    original_id = f0.id

    f1, task, action = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v2-longer", content_type="text/plain", parser_type="txt",
    )
    assert action == "updated"
    assert f1.id == original_id          # same row reused
    assert f1.uri == "/r.txt"
    assert f1.tag == "report"
    assert f1.size == len(b"v2-longer")  # content metadata refreshed
    assert task is not None
    # still exactly one active row with this tag
    assert db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).count() == 1


def test_upsert_update_ignores_multipart_filename(db, wsowner, stub_minio):
    """spec §4.8: same target_path is UPDATE even if the multipart filename differs;
    uri is driven by target_path, never by the uploaded filename."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # upsert_file_by_tag takes no filename param; target_path alone decides the uri.
    f1, _, action = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v2", content_type="text/plain", parser_type="txt",
    )
    assert action == "updated"
    assert f1.id == f0.id and f1.uri == "/r.txt"
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
Expected: PASS（3 passed：create + update + update-ignores-filename）。

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
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    original_id = f0.id

    f1, task, action = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/archive/r2.txt",
        file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert action == "moved"
    assert f1.id == original_id          # SAME row relocated, not a new one (tag/id preserved)
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
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    # an unrelated active file already sits at /archive/r2.txt (no tag)
    other = File(uri="/archive/r2.txt", name="r2.txt", owner_id=u.id, workspace_id=w.id,
                 is_directory=False, size=1)
    db.add(other); db.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 409
    # original tag/doc still reachable at its old uri
    assert db.query(File).filter(File.uri == "/r.txt", File.tag == "report",
                                 File.deleted_at.is_(None)).count() == 1


def test_upsert_move_rejects_pending_deleted_ancestor(db, wsowner, stub_minio):
    """Phase 2 write-guard: cannot relocate a tagged doc into a soft-deleted subtree."""
    w, u = wsowner
    upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
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
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 409


def test_upsert_move_requires_existing_parent_without_create_dirs(db, wsowner, stub_minio):
    """P2-a / spec §4.9: relocating to a path whose parent dir is not an active directory
    must 400 when create_dirs is not set (mirrors service-upload strict parent semantics)."""
    w, u = wsowner
    upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/missing/b.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt",  # create_dirs defaults False
        )
    assert ei.value.status_code == 400
    # original remains reachable & unmoved
    assert db.query(File).filter(File.uri == "/r.txt", File.tag == "report",
                                 File.deleted_at.is_(None)).count() == 1


def test_upsert_move_minio_put_failure_keeps_old_reachable(db, wsowner, stub_minio, monkeypatch):
    """spec §4.8/§6: if writing the NEW object fails, the DB is untouched and the original
    tag still resolves to the original document at its original uri (no lost-doc window)."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    file_ingest.ensure_directory_path(db, w, "/archive")  # parent exists so we reach put_file

    class _PutBoom:
        def put_file(self, *a, **k): raise RuntimeError("minio down")
        def remove_file(self, *a, **k): return None
        def remove_document_hierarchy(self, *a, **k): return None
    monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", lambda *a, **k: _PutBoom())

    with pytest.raises(RuntimeError):
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt",
        )
    db.expire_all()
    row = db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).first()
    assert row is not None and row.id == f0.id and row.uri == "/r.txt"  # original intact


def test_upsert_move_db_failure_rolls_back_and_keeps_old_reachable(db, wsowner, stub_minio, monkeypatch):
    """spec §4.8/§6: if the move's single-commit transaction fails, the row rolls back to
    its original uri/tag (never deleted), so the tag still resolves to a reachable doc."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="report", target_path="/r.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    file_ingest.ensure_directory_path(db, w, "/archive")

    def _boom(*a, **k):
        raise RuntimeError("db write failed")
    monkeypatch.setattr("openrag.services.task_service.TaskService.add_task", _boom)

    with pytest.raises(RuntimeError):
        upsert_file_by_tag(
            db, w, u.id, tag="report", target_path="/archive/r2.txt",
            file_content=b"v2", content_type="text/plain", parser_type="txt",
        )
    db.expire_all()
    row = db.query(File).filter(File.tag == "report", File.deleted_at.is_(None)).first()
    assert row is not None and row.id == f0.id and row.uri == "/r.txt"  # rolled back, reachable
```

- [ ] **Step 2: 跑测试确认失败**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: FAIL — `NotImplementedError`（move 分支未实现）。

- [ ] **Step 3: 实现 move+replace 分支**

先在 `file_ingest.py` 顶部 import 区补三个新依赖（move helper 单事务内/提交后所需，现状未 import）：

```python
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as FileModel, ProcessingStatus  # 现状只 import 了 File as FileModel
from openrag.services.file_deletion import delete_milvus_vectors_for_file
```

> `file_deletion` 不 import `file_ingest`，无循环依赖（Phase 2 已确认）。`ProcessingStatus` 与 `FileModel` 同源 `models.file`，把现有 `from openrag.models.file import File as FileModel` 一行改为同时引入 `ProcessingStatus` 即可。

在 `upsert_file_by_tag` 之前新增**专用单事务 move+replace helper**（spec §4.8，不串联 `replace_file_content`/`move_file`）：

```python
def _move_replace_no_intermediate_commit(
    db: Session,
    workspace: Workspace,
    acting_user_id: int,
    file: FileModel,
    target_uri: str,
    *,
    new_content: bytes,
    content_type: Optional[str],
    parser_type: str = "auto",
) -> Optional[Task]:
    """Same-row move+replace with NO intermediate commit (spec §4.8).

    Unlike chaining move_file + replace_file_content (each commits and touches storage
    several times), this keeps the failure window minimal: the File row is NEVER deleted
    and the tag is NEVER released, so on any failure the original tag still resolves to a
    reachable document. Sequence:
      1. validate parser/size/mime up front (no writes yet);
      2. write the NEW content to ``target_uri`` (if this fails, the DB is untouched);
      3. in ONE DB transaction: repoint the SAME row's uri/name/size/mime_type/parser_type/
         processing fields, delete its DocumentChunk rows, enqueue process_document via the
         non-committing add_task, then a single commit;
      4. on commit failure: rollback + best-effort delete the just-written new object;
      5. on commit success: best-effort cleanup of the OLD uri's object/hierarchy/vectors.
    """
    if parser_type not in SUPPORTED_PARSER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parser_type. Supported types: {', '.join(SUPPORTED_PARSER_TYPES)}",
        )
    file_size = len(new_content)
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds maximum allowed size of {MAX_FILE_SIZE / 1024 / 1024}MB",
        )
    effective_mime = resolve_effective_mime_type(
        content_type or file.mime_type, posixpath.basename(target_uri), parser_type
    )
    if effective_mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {effective_mime} is not supported for processing",
        )

    old_uri = file.uri
    minio_storage = MinioStorage()
    # (2) write NEW object first; if this raises, the DB is still untouched.
    minio_storage.put_file(workspace.slug, target_uri, new_content, content_type=effective_mime)

    # (3) single DB transaction: repoint the same row + clear chunks + enqueue, one commit.
    try:
        file.uri = target_uri
        file.name = posixpath.basename(target_uri)
        file.size = file_size
        file.mime_type = effective_mime
        file.parser_type = parser_type if parser_type != "auto" else None
        file.processing_status = ProcessingStatus.pending
        file.processing_error = None
        file.l0_path = None
        file.l1_path = None
        file.l2_path = None
        file.l0_vector_id = None
        file.total_chunks = 0
        file.total_tokens = 0
        db.query(DocumentChunk).filter(DocumentChunk.file_id == file.id).delete(
            synchronize_session=False
        )
        task = TaskService(db).add_task(
            workspace_id=file.workspace_id,
            user_id=acting_user_id,
            file_id=file.id,
            task_type="process_document",
            queue="normal",
            priority=5,
            max_retries=3,
            status=TaskStatus.PENDING,
        )
        db.commit()
        db.refresh(file)
    except Exception:
        db.rollback()  # (4) row reverts to old uri/tag; never deleted, tag never released
        try:
            minio_storage.remove_file(workspace.slug, target_uri)  # best-effort: drop just-written object
        except Exception:
            pass
        raise

    # (5) commit succeeded -> best-effort cleanup of the OLD uri's storage + vectors.
    try:
        minio_storage.remove_file(workspace.slug, old_uri)
    except Exception:
        pass
    try:
        minio_storage.remove_document_hierarchy(workspace.slug, old_uri)
    except Exception:
        pass
    try:
        delete_milvus_vectors_for_file(file.id)
    except Exception:
        pass
    return task
```

然后把 `upsert_file_by_tag` 末尾第二个 `raise NotImplementedError` 替换为（异路径 move 分支：先全部校验，再走专用 helper）：

```python
    # existing.uri != target_uri -> same-row move + replace (no intermediate commit).
    assert_no_pending_deleted_ancestor(db, workspace.id, target_uri)
    parent = posixpath.dirname(target_uri) or "/"
    if create_dirs:
        ensure_directory_path(db, workspace, parent)
    else:
        _assert_parent_directory_exists(db, workspace.id, parent)
    occupied = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace.id,
            FileModel.uri == target_uri,
            FileModel.deleted_at.is_(None),
        )
        .first()
    )
    if occupied is not None and occupied.id != existing.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target path already occupied by another document: {target_uri}",
        )
    task = _move_replace_no_intermediate_commit(
        db,
        workspace,
        owner_user_id,
        existing,
        target_uri,
        new_content=file_content,
        content_type=content_type,
        parser_type=parser_type,
    )
    db.refresh(existing)
    return existing, task, "moved"
```

> `assert_no_pending_deleted_ancestor`、`ensure_directory_path`、`_assert_parent_directory_exists`、`MinioStorage`、`posixpath`、`TaskService`、`TaskStatus`、`SUPPORTED_PARSER_TYPES`、`MAX_FILE_SIZE`、`ALLOWED_MIME_TYPES`、`resolve_effective_mime_type` 均已在 `file_ingest.py` 定义/import；`DocumentChunk`、`ProcessingStatus`、`delete_milvus_vectors_for_file` 由本步骤新增 import。move 分支**不调用** `replace_file_content`（避免其多次 commit + 中途存储副作用造成的丢失窗口）；`file.id`/`tag` 在 move 后不变（同一文档，非新建）。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: PASS（9 passed：create + update + update-ignores-filename + move + 目标占用 409 + pending 祖先 409 + 缺父目录 400 + MinIO 写失败原文档可达 + DB 失败回滚原文档可达）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/tests/test_file_ingest_upsert.py
git commit -m "feat(upsert): move+replace via dedicated single-commit helper (tag + diff uri, no lost-doc window)"
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


def _put_upsert(client, workspace, headers, *, tag, target_path, content, filename="upload.bin", parser="txt"):
    # tag + target_path are Form fields (spec §4.8); the multipart filename is decorative.
    return client.put(
        f"/service/v1/workspaces/{workspace.name}/documents/upsert-by-tag",
        data={"tag": tag, "target_path": target_path, "parser_type": parser, "create_dirs": "true"},
        files={"file": (filename, io.BytesIO(content), "text/plain")},
        headers=headers,
    )


def test_upsert_creates_returns_201(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/a.txt", content=b"hello")
    assert r.status_code == 201
    body = r.json()
    assert body["tag"] == "t1" and body["path"] == "/a.txt"
    assert body["action"] == "created"
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1


def test_upsert_update_in_place_returns_200(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    _put_upsert(client, workspace, service_token_write_headers,
                tag="t1", target_path="/a.txt", content=b"v1")
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/a.txt", content=b"v2-longer")
    assert r.status_code == 200
    assert r.json()["path"] == "/a.txt" and r.json()["action"] == "updated"
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1


def test_upsert_move_and_replace_returns_200(client, db, workspace, owner, service_token_write_headers):
    _root(db, workspace, owner)
    _put_upsert(client, workspace, service_token_write_headers,
                tag="t1", target_path="/a.txt", content=b"v1")
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/archive/b.txt", content=b"v2")
    assert r.status_code == 200
    assert r.json()["path"] == "/archive/b.txt" and r.json()["action"] == "moved"
    assert db.query(File).filter(File.tag == "t1", File.deleted_at.is_(None)).count() == 1
    assert db.query(File).filter(File.uri == "/a.txt", File.deleted_at.is_(None)).count() == 0


def test_upsert_target_path_drives_uri_not_multipart_filename(client, db, workspace, owner, service_token_write_headers):
    """spec §4.8/§8.5#10: the uri comes from Form target_path, NOT the multipart filename."""
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_write_headers,
                    tag="t1", target_path="/archive/real.txt", content=b"v1", filename="ignored.txt")
    assert r.status_code == 201
    assert r.json()["path"] == "/archive/real.txt"
    assert db.query(File).filter(File.uri == "/archive/real.txt", File.deleted_at.is_(None)).count() == 1
    assert db.query(File).filter(File.name == "ignored.txt").count() == 0


def test_upsert_missing_target_path_returns_422(client, db, workspace, owner, service_token_write_headers):
    """target_path is a required Form field; omitting it is a 422 (not a silent default)."""
    _root(db, workspace, owner)
    r = client.put(
        f"/service/v1/workspaces/{workspace.name}/documents/upsert-by-tag",
        data={"tag": "t1", "parser_type": "txt", "create_dirs": "true"},  # no target_path
        files={"file": ("a.txt", io.BytesIO(b"v1"), "text/plain")},
        headers=service_token_write_headers,
    )
    assert r.status_code == 422


def test_upsert_requires_write_token(client, db, workspace, owner, service_token_headers):
    """read-only token cannot upsert."""
    _root(db, workspace, owner)
    r = _put_upsert(client, workspace, service_token_headers,
                    tag="t1", target_path="/a.txt", content=b"v1")
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
    tag: str = Form(..., min_length=1, description="Per-workspace unique tag (idempotency key)"),
    target_path: str = Form(..., description="Full file logical path, e.g. /dir/name.pdf"),
    file: UploadFile = File(...),
    parser_type: str = Form(default="auto"),
    create_dirs: bool = Form(
        default=False,
        description="Create missing parent directories (mkdir -p) before upsert",
    ),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Idempotent-by-tag upsert: create (201) / update-in-place (200) / move+replace (200).

    ``target_path`` is the FULL file path; the uri is never derived from the multipart
    filename (spec §4.8/§8.5#10).
    """
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    body = await file.read()
    file_record, task_record, action = upsert_file_by_tag(
        db,
        ws,
        ws.owner_id,
        tag=tag,
        target_path=target_path,
        file_content=body,
        content_type=file.content_type,
        parser_type=parser_type,
        create_dirs=create_dirs,
    )
    status_code = status.HTTP_201_CREATED if action == "created" else status.HTTP_200_OK
    payload = _upload_response_dict(file_record, task_record.id if task_record else None)
    payload["action"] = action
    return JSONResponse(status_code=status_code, content=payload)
```

并确认 `service_api.py` 顶部已 import `upsert_file_by_tag`：把现有 `from openrag.services.file_ingest import (...)` 块追加 `upsert_file_by_tag`（与 `ingest_new_file`、`replace_file_content`、`validate_path` 并列）。`Form`、`File`、`UploadFile`、`JSONResponse`、`status` 均已在用（`tag`/`target_path` 为 Form 字段，不再用 `Query`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_service_api_upsert.py -v`
Expected: PASS（6 passed：create 201 / update 200 / move 200 / target_path 驱动 uri / 缺 target_path 422 / read-token 403）。

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
            db, w, u.id, tag="   ", target_path="/a.txt",
            file_content=b"x", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 400


def test_upsert_invalid_tag_charset_rejected_400(db, wsowner, stub_minio):
    w, u = wsowner
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        upsert_file_by_tag(
            db, w, u.id, tag="bad/tag", target_path="/a.txt",
            file_content=b"x", content_type="text/plain", parser_type="txt", create_dirs=True,
        )
    assert ei.value.status_code == 400


def test_upsert_treats_soft_deleted_tag_as_create(db, wsowner, stub_minio):
    """A soft-deleted row has tag=NULL, so its old tag is free -> upsert creates anew."""
    w, u = wsowner
    f0, _, _ = upsert_file_by_tag(
        db, w, u.id, tag="reuse", target_path="/a.txt",
        file_content=b"v1", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    from openrag.services.file_deletion import utcnow
    f0.deleted_at = utcnow(); f0.tag = None  # simulate Phase 2 soft delete
    db.commit()

    f1, _, action = upsert_file_by_tag(
        db, w, u.id, tag="reuse", target_path="/b.txt",
        file_content=b"v2", content_type="text/plain", parser_type="txt", create_dirs=True,
    )
    assert action == "created"
    assert f1.id != f0.id and f1.tag == "reuse" and f1.uri == "/b.txt"
    assert db.query(File).filter(File.tag == "reuse", File.deleted_at.is_(None)).count() == 1
```

- [ ] **Step 2: 跑测试确认结果**

Run: `& "E:\project\OpenRag\openrag\venv\Scripts\python.exe" -m pytest tests/test_file_ingest_upsert.py -v`
Expected: 全 PASS（12 passed）。空/非法 tag 由 `_normalize_tag` + upsert 入口 400；软删 tag 因 `tag=NULL` 落入 create——**这些行为已由 Task 1–3 的实现覆盖，本任务仅加回归确认，无需改实现**。若 `test_upsert_empty_tag_rejected_400` 意外失败，核对 `upsert_file_by_tag` 入口的 `if normalized_tag is None: raise HTTPException(400, ...)`。

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
- **合约 `target_path`**：uri 由 Form `target_path`（完整文件路径）决定，**与 multipart 文件名无关**（改上传文件名不会误触发 move）；缺 `target_path` → `422`。
- **create**：tag 不存在 → `201`、`action="created"`，新行带 tag、落在 `target_path`、入队 `process_document`。
- **update-in-place**：tag 存在且 `target_path` == 现有 uri → `200`、`action="updated"`，**同一行**内容替换、size/mime 刷新、重新入队；tag/uri 不变。
- **move+replace**：tag 存在但 `target_path` != 现有 uri → `200`、`action="moved"`，**同一行（file.id/tag 不变）**搬到新 uri（name 随之改）、内容替换、旧 uri 不再有 active 行；提交成功后旧 uri 源对象/层级/向量已 best-effort 清理。
- **move 失败语义**（spec §4.8/§6）：MinIO 写新对象失败 → DB 不变；move 事务失败 → 回滚到旧 uri；两种情况下**绝不删行/释放 tag**，原 tag 仍解析到可达的原文档。
- **幂等键唯一性**：任一分支后，`(workspace, tag, deleted_at IS NULL)` 始终恰好 1 行。
- **冲突**：目标 uri 被另一 active 文件占用 → `409`；搬入软删子树（pending 祖先，含 target_uri 自身）→ `409`；create 撞 uri → `409`（`ingest_new_file` 既有分类）。
- **父目录语义**：move 到父目录不存在的路径，`create_dirs=False` → `400`，`create_dirs=True` 自动建父目录后成功。
- **校验继承**：空/非法 tag → `400`；超 `MAX_FILE_SIZE` → `413`；非法 `parser_type`/不支持 MIME → `400`。
- **权限**：read-only service token → `403`。
- **软删 tag 复用**：tag 被软删释放后，upsert 走 create 分支重新铸造。

- [ ] **Step 2: 标记阶段完成**

Phase 3 完成。三阶段（Phase 1 tag 唯一性 / Phase 2 软删除 / Phase 3 upsert-by-tag）合起来构成「单文档唯一 tag」的完整生命周期：建→查→改/搬→删→tag 复用。

- [ ] **Step 3: 收尾**

调用 superpowers:finishing-a-development-branch 决定整分支去向（合并 / PR / 保留 / 丢弃）。

---

## Self-Review（计划自查）

- **Spec 覆盖**：以 `target_path` 为合约（spec §4.8/§8.5#10，Task 1/4）；三分支 create/update/move（Task 1/2/3）；专用单事务 move helper 的失败语义（spec §4.8 L151-157，Task 3）；`create_dirs=False` 父目录校验（spec §4.9，Task 3）；端点按 `action` 映射 201/200（spec §4.8，Task 4）；边界（tag 校验、软删复用）Task 5；全量回归 Task 6。✅
- **复用而非重造（DRY/YAGNI）**：create 复用 `ingest_new_file`、update 复用 `replace_file_content`；**move 用专用 `_move_replace_no_intermediate_commit`，不串联 `replace_file_content`/`move_file`**（spec 明令，避免多次 commit 的丢失窗口）；写守卫复用 Phase 2 `assert_no_pending_deleted_ancestor`；无新列/无新迁移。✅
- **占位符扫描**：每个实现步骤含完整代码与精确锚点；每个测试步骤含完整测试代码与可跑命令；Task 5 显式标注「多为验证既有行为、按需改实现」。无 TBD/TODO。✅
- **类型/签名一致性**：`upsert_file_by_tag(db, workspace, owner_user_id, *, tag, target_path, file_content, content_type, parser_type="auto", create_dirs=False) -> Tuple[FileModel, Optional[Task], str]` 在 Task 1 定义、Task 2/3 续写、Task 4 端点调用三处一致（**无** `parent_logical_path`/`upload_filename`）；`action ∈ {"created","updated","moved"}` 与端点 201/200 映射、响应体 `payload["action"]`、测试断言四处一致；`_move_replace_no_intermediate_commit`、`_active_tagged_file` 签名与调用一致。✅
- **承接 Phase 1/2**：tag 查重依赖软删行 `tag=NULL`（Phase 2 保证）；`deleted_at IS NULL` 活跃判定贯穿；`replace_file_content` 仍是 `PUT by-path` 与本端点 update 分支共用。✅
- **已知边界/精确点**：move 分支沿用 `move_file` 先例不改 `parent_id`（uri 为真相源）；move helper **先写新对象→单事务改行+删 chunk+`add_task`→单 commit→失败回滚并删新对象→成功后清旧对象/层级/向量**，绝不删行/释放 tag，故任一步失败原 tag 与原文档至少一者可达；`target_path` 始终为完整文件路径，create 分支以其 basename 作文件名。
- **测试约定**：沿用 Phase 1/2 实测的 venv/pythonpath/跨文件 fixture 复用/`_root()` 种子；服务层单测 stub MinIO + milvus 共四处（`file_ingest.MinioStorage`、`file_ingest.delete_milvus_vectors_for_file`、`files_api.MinioStorage`、`files_api.delete_milvus_vectors_for_file`）——move helper 在 `file_ingest` 命名空间调 milvus 清理，update 分支经 `replace_file_content→cleanup_file_processing_data` 触达 files_api 的 MinIO/milvus。

---

## 待执行时复核的小风险（执行者注意）

- **move 的失败语义（已按 spec 收敛，非降级）**：move 分支用专用 `_move_replace_no_intermediate_commit`——元数据更新 + 删 chunk + 建任务在**单次 DB 事务**内提交；commit 前先写新对象，commit 失败 rollback 并删新对象，commit 成功后才清旧对象/层级/向量。因 MinIO/Milvus 与 DB 无法跨系统原子提交，承诺限定为「**失败时不删行、不释放 tag**，原 tag 与原文档至少一者可达」（spec §4.8 L157）。**不复用** `replace_file_content` 的多次 commit 流程（那会重新引入 Codex P1-b 的丢失窗口）。剩余的「提交后清理旧对象失败」只是旧对象残留（日志/重试/后续清理处理），不影响新文档可达性。
- **service 层单测 stub 完整性**：upsert 路径会 new `MinioStorage()`、调 `delete_milvus_vectors_for_file` 于**两个命名空间**——`file_ingest`（move helper + create）与 `files_api`（update 经 `replace_file_content→cleanup_file_processing_data`）。`stub_minio` fixture 已 patch 全部四处。若执行时报真实 MinIO/Milvus 连接错，核对这四个 patch 目标的模块路径。

---

## Codex 审查补充（2026-06-30，来自 Codex）

> **【已核实并全部采纳，2026-06-30】** 四点均对照权威 spec（`docs/superpowers/specs/2026-06-26-single-file-unique-tag-design.md` §4.8/§4.9/§6/§8.5）核实属实，已改入正文：
> - **P1-a** → `upsert_file_by_tag` 与端点改用 Form `tag` + 完整 `target_path`（Task 1/4），create 分支由 `target_path` 的 dirname/basename 喂 `ingest_new_file`；新增「同 target_path 改文件名仍 update」「缺 target_path 422」回归。
> - **P1-b** → 新增专用 `_move_replace_no_intermediate_commit`（先写新对象→单事务改行+删 chunk+`add_task`→单 commit→失败回滚并删新对象→成功清旧），**不串** `replace_file_content`（Task 3）；新增 MinIO 写失败 / DB 失败后原文档仍可达回归。
> - **P2-a** → move 分支 `create_dirs=True` 走 `ensure_directory_path`、`False` 走 `_assert_parent_directory_exists`（缺父目录 400，Task 3）。
> - **P2-b** → 返回字段统一为 `action`，值 `created`/`updated`/`moved`（Task 1/3/4，全文同步）。
> 下方为审查原文，保留供追溯；执行以正文为准。

结论：当前计划不能直接进入执行。总体方向「workspace 内 tag 唯一、按 tag create/update/move、复用 Phase 1/2 软删除语义」是成立的，但计划细节与现行 spec 的 Phase 3 主线存在两处实质偏离，会影响外部 API 合约和 move 失败语义。建议先修正计划，再执行代码落地。

### P1：接口语义偏离 spec，`target_path` 被退回成 `path + file.filename`

spec §4.8 已明确 `PUT /service/v1/workspaces/{workspace_name}/documents/upsert-by-tag` 使用 Form 字段 `tag` 与 `target_path`，其中 `target_path` 是完整文件路径，例如 `/dir/name.pdf`，不再由 multipart 的 `file.filename` 隐式决定 uri。当前计划仍定义为 `tag` Query 参数、`path` Form 父目录、`file.filename` 文件名，并在 `upsert_file_by_tag()` 中用 `build_file_uri(validate_path(parent_logical_path), upload_filename)` 得到目标 uri。

这个差异不是文档措辞问题，而是会改变幂等行为：同一个 tag、同一个业务目标文件，如果调用方只改变上传文件名，就会被误判为异路径 move；反过来调用方也无法只通过显式 `target_path` 稳定声明目标 uri。外部客户端若按 spec 集成会传 `target_path`，但当前计划实现的端点不会识别该字段。

修正建议：

- `service_upsert_document_by_tag()` 改为接收 `tag: str = Form(...)`、`target_path: str = Form(...)`，不要使用 Query `tag` 和父目录 `path`。
- `upsert_file_by_tag()` 签名改为以 `target_path` 为输入，在函数内部执行 `target_uri = validate_path(target_path)`。
- create 分支复用 `ingest_new_file()` 时由 `target_uri` 拆出 `parent_logical_path = posixpath.dirname(target_uri) or "/"`、`upload_filename = posixpath.basename(target_uri)`。
- 测试补充「同一 `target_path` 但 multipart filename 不同仍是 update，不触发 move」和「请求只传 `path` 不传 `target_path` 应失败」。

### P1：move+replace 直接串联 `_relocate_active_file()` 与 `replace_file_content()`，与 spec 的失败语义冲突

spec §4.8 已明确 move+replace 不应直接串联现有 `move_file`/`replace_file_content()` 这类内部带存储副作用和多次 commit 的流程，而是需要专用 helper：先校验、先写新对象，随后在单次 DB 事务中更新同一行元数据、清 chunks、创建任务，commit 成功后再 best-effort 清理旧对象/旧层级/旧向量。当前计划的 `_relocate_active_file()` 会先 best-effort 删除旧对象和旧 hierarchy，再把 `file.uri` 改到目标路径并 commit，之后调用 `replace_file_content()`；而 `replace_file_content()` 又会先 cleanup + commit，再写新对象，再 commit。

这会产生实际破坏窗口：如果 `_relocate_active_file()` 已提交 uri 搬移、旧对象也已删除，而后续 `replace_file_content()` 在 MinIO `put_file()` 或后续 commit 失败，tag 仍在同一行上，但文件已经从旧路径搬走，目标路径可能没有新对象，调用方无法通过原 tag 找回原文档内容。这正是 spec 要避免的「移动/替换失败时原 tag 与原文档可达性不丢」。

修正建议：

- 不把该风险降级为「沿用既有 replace 非原子性」。Phase 3 的异路径 upsert 是新增破坏性能力，spec 已单独收敛了更强的失败语义。
- 新增专用 move+replace helper，最小实现也应满足：所有校验先完成；新内容先写入 `target_uri`；DB 事务内更新同一 `File` 行的 `uri/name/size/mime_type/parser_type/processing` 字段，删除 `DocumentChunk(file_id=...)`，通过不主动 commit 的任务创建方式入队 `process_document`，然后单次 commit；commit 失败 rollback 并 best-effort 删除刚写入的新对象；commit 成功后再 best-effort 清理旧 uri 的对象、层级和向量。
- 测试补充 MinIO 写新对象失败、DB commit 失败两类用例，断言失败后旧 `file.id/tag/uri` 仍可达，且不会留下「行已搬、内容未写」状态。

### P2：move 分支缺少 `create_dirs=False` 时的父目录存在校验

create 分支通过 `ingest_new_file(require_parent_dir=not create_dirs)` 继承了现有严格语义：`create_dirs=False` 时父目录必须已存在。但 move 分支的 `_relocate_active_file()` 只有在 `create_dirs=True` 时调用 `ensure_directory_path()`，`create_dirs=False` 时没有调用 `_assert_parent_directory_exists()`，因此可能把 active 文件搬到一个不存在的父目录下，形成不可导航的孤儿路径。这与 service upload 的 `create_dirs` 语义和 spec 中「目标父目录必须是 active 目录」冲突。

修正建议：

- 在 move helper 中统一计算 `parent = posixpath.dirname(target_uri) or "/"`。
- `create_dirs=True` 时调用 `ensure_directory_path(db, workspace, parent)`；`create_dirs=False` 时调用 `_assert_parent_directory_exists(db, workspace.id, parent)`。
- 增加测试：tag 已存在，move 到 `/missing/b.txt` 且不传 `create_dirs` 返回 400；预先创建 `/missing` 或传 `create_dirs=true` 才允许成功。

### P2：返回字段和值需要和 spec/API 文档统一

spec 写的是返回 `action`，表格值为 `created` / `updated` / `moved`；当前计划返回字段为 `outcome`，move 值为 `moved_replaced`。二者都能表达状态，但外部 API 合同必须唯一。建议执行前二选一并同步 plan/spec/API 文档/测试。若保持当前计划的 `outcome=moved_replaced`，必须显式改 spec；若遵循 spec，则端点返回 `action`，move 值用 `moved`。

### 测试补充建议

- service 层新增 `target_path` 合约测试：Form 传 `target_path=/archive/b.txt`，multipart filename 即使是 `ignored.txt`，最终 `File.uri` 仍为 `/archive/b.txt`。
- service 层断言返回动作字段和值，避免实现与文档漂移。
- move 分支新增 `create_dirs=False` 缺父目录失败、`create_dirs=True` 自动建父目录成功。
- move+replace 失败语义新增 MinIO put 失败和 DB commit 失败测试，覆盖「旧 tag 与旧文档仍可达」。
- 保留目标路径被 active 文件占用、目标路径/祖先 pending deletion 返回 409 的测试。现有 `assert_no_pending_deleted_ancestor()` 会把 `target_uri` 本身纳入 candidates，因此软删目标 uri 理论上已能命中 pending deletion，不需要额外扩大为新逻辑。

### 范围判断

计划触达文件范围本身是收敛的：主要是 `file_ingest.py`、`service_api.py` 和对应测试，不需要新迁移，也不需要扩到前端或检索链路。真正需要调整的是 Phase 3 的目标路径 API 合同和 move+replace 的服务层 helper 边界。修正以上 P1/P2 后，主体方案可以进入执行。
