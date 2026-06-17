# 服务令牌自动建目录(mkdir -p)实施计划 —— 修复 O3「Parent directory does not exist」

> **给执行者(含 AI agent):** 必需子技能 —— 用 superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 按任务逐条执行。所有步骤用 `- [ ]` 复选框跟踪。

**目标:** 让 service token 客户端能把文档上传到一个尚不存在的逻辑路径——通过新增一个**可选**的 `create_dirs` 开关,在上传前自动补建父目录(`mkdir -p`),从而解除 O3 对"全自动个人知识库"流程的硬阻塞。

**总体思路:** 在 `file_ingest.py` 里加一个**幂等、防并发**的服务层 helper `ensure_directory_path()`,它会创建工作区根 `/` 以及路径上每一级缺失的 `File(is_directory=True)` 行(完全对齐现有 JWT 的 `create_directory`:纯数据库虚拟目录,不写 MinIO 对象)。再把它接到 service token 上传端点上,藏在 `create_dirs: bool = Form(default=False)` 开关后面。缺省 `False` → 现网所有调用方行为**完全不变**;`True` → 先把深层路径建好,文件落下后即可在目录树里被导航到。

**技术栈:** Python 3、FastAPI、SQLAlchemy ORM、pytest + FastAPI `TestClient`、内存 SQLite(测试)、MinIO(测试中 mock)。

---

## ✅ 执行结果（回填 · 2026-06-17）

**状态:已完成并落地到分支 `fix/o3-service-token-mkdir-p`(基线 `c0910677`)。** 用 subagent 驱动 + 两段式评审执行;O3 阻塞已解除。

### 提交记录(5 个提交,仅改动计划中的 4 个文件)

| 提交 | 内容 |
|------|------|
| `bd6748b` | Task 1:`ensure_directory_path`(mkdir -p)helper + 4 单元测试 |
| `29127c4` | Task 2:上传端点 `create_dirs` 开关 + 3 集成测试 + 启动桩 fixture |
| `064d54b` | Task 2 评审加固:并发 bounded retry、引擎单例单点 patch、依赖注释 |
| `1bc3bbe` | Task 4:文档化 `create_dirs` |
| `cdb10c2` | 终评修复:竞争耗尽返回干净 503、`override_get_db` 回滚 |

改动文件:[file_ingest.py](../../../openrag/src/openrag/services/file_ingest.py)、[service_api.py](../../../openrag/src/openrag/api/service_api.py)、[test_service_api.py](../../../openrag/tests/test_service_api.py)、[04-…API.md](../../../docs/04-外部系统接入与API.md)。无关文件零波及。

### 测试证据

- `tests/test_service_api.py` **38 passed**(31 既有 + 4 helper 单测 + 3 端点集成测试),~11s。严格 TDD:每个测试先看红再实现转绿。
- 跨模块回归:`test_service_api.py + test_files_api.py + test_workspace_file_tree.py` 中,仅 `test_files_api.py` 有 5 个 **预先存在** 的失败 —— 均为 `ConnectionRefusedError localhost:9000`(本地无 MinIO),在基线 `c0910677` 上**同样失败**(已逐一验证),与本次改动无关。

### 评审结论

- **Task 1**:规格 ✅;代码质量 **APPROVED** —— 已采纳:删除未用 `parent_id` 形参、强化幂等测试、修正 import 顺序。
- **Task 2**:规格 ✅;代码质量 **APPROVED** —— 已采纳:`_engine` 单例单点 patch、并发 bounded retry、`require_parent_dir` 依赖注释。
- **终评**:CHANGES REQUIRED → 已逐条甄别后采纳有效项(竞争耗尽 503、测试会话回滚);驳回无效/越界项(误报的 UnboundLocalError、`mkdir -p` 语义下的"残留空目录"为设计内、`owner_id` 与既有上传一致、`test_import_d_data_folder_script.py` 系他人未跟踪 WIP)。

### 与原计划的偏差(及原因)

1. **新增 `_stub_app_startup` autouse fixture(计划外,必要基建)** —— 执行中发现:应用启动事件 `@app.on_event("startup")` 直接调用 `get_db()`(绕过依赖覆盖)并 `SELECT 1`,会连真实 Postgres 而在本地**挂死**,导致所有 `TestClient` 测试无法本地运行。fixture 把惰性引擎单例 `openrag.database._engine` 指向测试内存 SQLite 并打桩 scheduler,使端点测试可在无外部基建下运行。根因(启动绕过 DI)属应用既有问题,单独跟踪。
2. **Task 3(建工作区播种根目录)按计划与建议跳过** —— helper 的 `get_or_create_root` 已按需兜底,Task 3 收益小、回归面大。
3. **一次 fix subagent 遇基础设施鉴权错误(403)** —— 该轮改动改由控制器直接应用并验证(改动微小且已完整指定),其余实现/评审均为 subagent。
4. **遗留(单独 issue,未纳入本次):** `File.parent_id` 在上传/建目录时全程不写(影响权限继承与 L0/L1 汇总);本 helper 刻意对齐既有 `create_directory` 不写 `parent_id`,避免引入新不一致。

### 验收(O3 是否真的解通)

`test_service_upload_create_dirs_materialises_parents`:无任何目录(连根都没有)时,service token 带 `create_dirs=true` 上传到 `/personal/u1/kb1` → **201**,文件落在 `/personal/u1/kb1/n.txt`,且 `/personal`、`/personal/u1`、`/personal/u1/kb1` 三级目录行均生成;`test_..._file_is_navigable` 进一步确认文件在 `children` 导航中可见(非孤儿)。缺省不带 `create_dirs` 时仍返回严格 **400**(`test_..._without_create_dirs_still_400`)。**O3 解除。**

---

## 一、现在的问题

外部业务系统(GZClaw)持 **service token** 调 OpenRAG,想把文档写进一个"个人知识库"。个人知识库在它那边是一个**虚拟路径前缀**(例如 `/personal/u1/kb1`),并不对应独立工作区。

实际调用:

```
POST /service/v1/workspaces/{workspace_name}/documents
  Header: X-OpenRag-Token: sk-...
  Form:   path=/personal/u1/kb1
          file=<二进制>
```

**结果:返回 `400 Parent directory does not exist`,文件传不上去。**

- 这是目前清单里**唯一的真·硬阻塞(O3)**。
- 关键困境:GZClaw 只有 service token,而它**没有任何 service 端点可以先把 `/personal/u1/kb1` 这个目录建出来**——于是陷入"上传要求目录先存在,却又无法用 service token 把目录建出来"的死锁。

> 对比 RagFlow(dataset 是扁平一等实体、无目录概念)不会有这问题。O3 是 OpenRAG 的目录模型 + 凭证边界共同造成的,属 OpenRAG 专属,GZClaw 单方面修不了。

## 二、问题产生的原因(已逐行在代码里坐实)

1. **service token 上传写死了"父目录必须先存在"。**
   端点 `service_upload_document` 调 `ingest_new_file(...)` 时硬编码 `require_parent_dir=True` —— [service_api.py:310](../../../openrag/src/openrag/api/service_api.py#L310)。

2. **父目录校验连根 `/` 都要求有目录行。**
   [`_assert_parent_directory_exists`](../../../openrag/src/openrag/services/file_ingest.py#L226) 在 `files` 表里找 `workspace_id + uri==父路径 + is_directory=True` 的行,找不到就抛 400 —— [file_ingest.py:251](../../../openrag/src/openrag/services/file_ingest.py#L251)。对 `/` 也不放过。

3. **全仓库只有 JWT 能建目录行,service API 没有建目录端点。**
   唯一的建目录入口是 [`create_directory`](../../../openrag/src/openrag/api/files_api.py#L1015)(`POST /files/directories`),依赖 `Depends(get_current_user)`,即 **JWT 用户态**。而 service API 那一组 11 个端点(全部 `get_service_token_context`)里**没有任何建目录端点**。

4. **两类凭证明令禁止混用。**
   service 路由只认 `X-OpenRag-Token`,用户路由用 OAuth2 Bearer JWT,互不兼容。→ 拿 service token 的客户端**调不了**那个 JWT 建目录端点。

5. **建工作区时不播种根目录行。**
   [`create_workspace`](../../../openrag/src/openrag/services/workspace_service.py#L15) 只建 `Workspace` + `WorkspaceMember`,**不建** `uri="/"` 的根目录行。所以全新工作区严格按代码连往根 `/` 上传都会失败,直到有人通过 Web/JWT 建了目录。

6. **导航树靠"目录行链"撑起,所以不能只是跳过校验。**
   [`list_direct_children`](../../../openrag/src/openrag/services/workspace_file_tree.py#L73) 只返回"直接父路径匹配"的行,[`build_nested_tree`](../../../openrag/src/openrag/services/workspace_file_tree.py#L229) 只往已存在的目录行下钻。如果上传 `/a/b/c.pdf` 时不建 `/a`、`/a/b` 目录行,这个文件会变成**孤儿**:能检索到、但在 tree/children 导航里**看不见**。→ **修复必须真正建出目录行,而不是简单地把校验关掉。**

**一句话根因:** 目录行只能由 JWT 用户态创建,而 service token 上传又强制父目录先存在——service API 压根没给机读客户端留"建目录"这个口子。个人知识库用虚拟路径前缀,是这条结构性矛盾最先撞墙的场景。

**本次明确不做(单独跟踪):** `parent_id` 在上传与建目录时全程不写(latent 问题,影响权限继承与 L0/L1 层级汇总)。本 helper 刻意对齐现有 `create_directory`,**不**写 `parent_id`,以免引入新的不一致。不要把 `parent_id` 修复塞进本计划。

## 三、文件改动总览

- **改** `openrag/src/openrag/services/file_ingest.py` —— 加 `IntegrityError` 导入 + `_get_or_create_directory_row`、`_ensure_directory_path_one_pass`、`ensure_directory_path`。(放这里很自然:`validate_path`/`build_file_uri` 本就在此文件。)
- **改** `openrag/src/openrag/api/service_api.py` —— 导入 `ensure_directory_path`;给 `service_upload_document` 加 `create_dirs` 开关;调 helper 并传 `require_parent_dir=not create_dirs`。
- **改** `openrag/tests/test_service_api.py` —— 加 helper 单元测试 + 端点集成测试。(此文件在 `pytest.ini` 的 `python_files` 白名单内,且已具备全部 fixture;**新建测试文件不会被收集**。)
- **改** `docs/04-外部系统接入与API.md` —— 文档化 `create_dirs` 参数并更新排障表。
- **(可选)改** `openrag/src/openrag/services/workspace_service.py` —— 建工作区时播种根 `/`(让 JWT/Web 路径更自洽;**修 O3 非必需**)。

## 四、约定

- **测试一律在 `openrag/` 目录下跑**(`pytest.ini` 在此;`conftest.py` 把 `.` 和 `src` 加进 path)。下文命令默认 cwd = `e:\project\OpenRag\openrag`。
- 运行器:`uv run pytest …`(本仓库用 `uv`)。若不用 uv,换成 `python -m pytest …`。
- 只有这些测试文件会被收集:`test_service_api.py`、`test_files_api.py`、`test_workspace_file_tree.py`、`test_service_token_*.py`、`test_service_tokens_admin.py`、`test_workspace_es_slug.py`、`test_import_d_data_folder_script.py`、`test_*_parity.py`。

---

## 五、具体执行步骤

### 任务 0:开 feature 分支

当前在 `main`,动手前先开分支。

- [ ] **步骤 1:创建并切换分支**

```bash
git checkout -b fix/o3-service-token-mkdir-p
```

---

### 任务 1:`ensure_directory_path` 自动建目录 helper(`mkdir -p`)

**文件:**
- 改:`openrag/src/openrag/services/file_ingest.py`(导入在第 11 行附近;新函数加在 `build_file_uri` 之后,约第 137 行)
- 测试:`openrag/tests/test_service_api.py`(加导入 + 4 个单元测试)

- [ ] **步骤 1:先写会失败的单元测试**

在 `openrag/tests/test_service_api.py` 顶部(与现有导入并列)加:

```python
from fastapi import HTTPException
from openrag.services.file_ingest import ensure_directory_path
```

在 `openrag/tests/test_service_api.py` 末尾追加:

```python
def test_ensure_directory_path_creates_nested_dirs(db: Session, workspace: Workspace, owner: User) -> None:
    leaf = ensure_directory_path(db, workspace, "/personal/u1/kb1")
    assert leaf.uri == "/personal/u1/kb1"
    assert leaf.is_directory is True
    for uri in ("/", "/personal", "/personal/u1", "/personal/u1/kb1"):
        row = (
            db.query(File)
            .filter(File.workspace_id == workspace.id, File.uri == uri, File.is_directory.is_(True))
            .first()
        )
        assert row is not None, f"missing directory row {uri}"


def test_ensure_directory_path_is_idempotent(db: Session, workspace: Workspace, owner: User) -> None:
    ensure_directory_path(db, workspace, "/a/b")
    ensure_directory_path(db, workspace, "/a/b")
    count = (
        db.query(File)
        .filter(File.workspace_id == workspace.id, File.uri == "/a")
        .count()
    )
    assert count == 1


def test_ensure_directory_path_seeds_root(db: Session, workspace: Workspace, owner: User) -> None:
    assert (
        db.query(File).filter(File.workspace_id == workspace.id, File.uri == "/").first()
        is None
    )
    ensure_directory_path(db, workspace, "/x")
    root = db.query(File).filter(File.workspace_id == workspace.id, File.uri == "/").first()
    assert root is not None and root.is_directory is True


def test_ensure_directory_path_rejects_file_in_path(db: Session, workspace: Workspace, owner: User) -> None:
    db.add(File(uri="/a", name="a", owner_id=owner.id, workspace_id=workspace.id, is_directory=False, size=3))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        ensure_directory_path(db, workspace, "/a/b")
    assert exc.value.status_code == 409
```

- [ ] **步骤 2:跑测试,确认是红的**

```bash
uv run pytest tests/test_service_api.py -k ensure_directory_path -v
```

预期:收集报错 / FAIL —— `ImportError: cannot import name 'ensure_directory_path' from 'openrag.services.file_ingest'`。

- [ ] **步骤 3:实现 helper**

在 `openrag/src/openrag/services/file_ingest.py`,在现有 `from sqlalchemy.orm import Session` 旁加导入:

```python
from sqlalchemy.exc import IntegrityError
```

在 `build_file_uri` 之后(第 137 行后)加这三个函数:

```python
def _get_or_create_directory_row(
    db: Session,
    workspace: Workspace,
    uri: str,
    name: str,
    parent_id: Optional[int],
) -> FileModel:
    """返回 ``workspace`` 下 ``uri`` 处的目录行,不存在则创建。

    对齐 ``files_api.create_directory``(纯数据库虚拟目录,不写 MinIO 对象)。
    若 ``uri`` 已作为「文件」而非目录存在 → 抛 409。并发插入竞态下可能抛
    ``IntegrityError``,由外层包装函数回滚后重试一次处理。
    """
    row = (
        db.query(FileModel)
        .filter(FileModel.workspace_id == workspace.id, FileModel.uri == uri)
        .first()
    )
    if row is not None:
        if not row.is_directory:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Path component already exists as a file: {uri}",
            )
        return row
    row = FileModel(
        uri=uri,
        name=name,
        owner_id=workspace.owner_id,
        workspace_id=workspace.id,
        is_directory=True,
        size=0,
    )
    db.add(row)
    db.flush()  # 赋值 row.id;竞态下可能抛 IntegrityError
    return row


def _ensure_directory_path_one_pass(
    db: Session, workspace: Workspace, path: str
) -> FileModel:
    """创建根 + 路径上每一级目录;返回最深的目录行。"""
    parent = _get_or_create_directory_row(db, workspace, "/", "root", None)
    if path != "/":
        cumulative = ""
        for part in [p for p in path.split("/") if p]:
            cumulative = f"{cumulative}/{part}"
            parent = _get_or_create_directory_row(
                db, workspace, cumulative, part, parent.id
            )
    return parent


def ensure_directory_path(
    db: Session, workspace: Workspace, logical_path: str
) -> FileModel:
    """幂等地创建 ``logical_path`` 及其所有缺失祖先目录(``mkdir -p``)。

    返回最深目录行。若工作区根 ``/`` 缺失则一并播种。并发安全:丢失的插入
    竞态会重试一次,届时每一级都已存在、不再发生插入。
    """
    path = validate_path(logical_path)
    try:
        parent = _ensure_directory_path_one_pass(db, workspace, path)
    except IntegrityError:
        db.rollback()
        parent = _ensure_directory_path_one_pass(db, workspace, path)
    db.commit()
    return parent
```

- [ ] **步骤 4:跑测试,确认变绿**

```bash
uv run pytest tests/test_service_api.py -k ensure_directory_path -v
```

预期:4 passed。

- [ ] **步骤 5:提交**

```bash
git add openrag/src/openrag/services/file_ingest.py openrag/tests/test_service_api.py
git commit -m "feat(file_ingest): add ensure_directory_path (mkdir -p) helper

Idempotent, race-safe creation of a logical path and all missing
ancestor directory rows, seeding workspace root. Mirrors the JWT
create_directory semantics (DB-only virtual dir). Unblocks O3.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### 任务 2:把 `create_dirs` 开关接到 service 上传端点

**文件:**
- 改:`openrag/src/openrag/api/service_api.py`(导入第 24 行;`service_upload_document` 第 290-313 行)
- 测试:`openrag/tests/test_service_api.py`(3 个集成测试)

- [ ] **步骤 1:先写会失败的集成测试**

在 `openrag/tests/test_service_api.py` 末尾追加:

```python
def test_service_upload_create_dirs_materialises_parents(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_write_headers
) -> None:
    # 完全不预置目录——连根都没有。
    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/personal/u1/kb1", "parser_type": "auto", "create_dirs": "true"}
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "/personal/u1/kb1/n.txt"
    for uri in ("/personal", "/personal/u1", "/personal/u1/kb1"):
        row = (
            db.query(File)
            .filter(File.workspace_id == workspace.id, File.uri == uri, File.is_directory.is_(True))
            .first()
        )
        assert row is not None, f"expected directory {uri} to be created"


def test_service_upload_without_create_dirs_still_400(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_write_headers
) -> None:
    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/personal/u1/kb1", "parser_type": "auto"}  # 不带 create_dirs -> 维持严格
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "Parent directory does not exist"


def test_service_upload_create_dirs_file_is_navigable(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_write_headers
) -> None:
    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/personal/u1/kb1", "create_dirs": "true"}
        up = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
        assert up.status_code == 201, up.text
        children = client.get(
            f"/service/v1/workspaces/{workspace.name}/children",
            params={"path": "/personal/u1/kb1"},
            headers=service_token_write_headers,
        )
    assert children.status_code == 200, children.text
    names = [item.get("name") for item in children.json()]
    assert "n.txt" in names
```

- [ ] **步骤 2:跑测试,确认是红的**

```bash
uv run pytest tests/test_service_api.py -k "create_dirs or without_create_dirs" -v
```

预期:`test_service_upload_create_dirs_materialises_parents` 因 `create_dirs` 当前被忽略而以 400(`Parent directory does not exist`)FAIL;`test_service_upload_create_dirs_file_is_navigable` 在 201 断言处 FAIL。(`test_service_upload_without_create_dirs_still_400` 已通过——它锁死现状。)

- [ ] **步骤 3:实现端点改动**

在 `openrag/src/openrag/api/service_api.py`,把第 24 行导入改为:

```python
from openrag.services.file_ingest import (
    ensure_directory_path,
    ingest_new_file,
    replace_file_content,
    validate_path,
)
```

把 `service_upload_document` 函数体(第 290-313 行)替换为:

```python
async def service_upload_document(
    workspace_name: str,
    path: str = Form(..., description="Parent directory logical path"),
    file: UploadFile = File(...),
    parser_type: str = Form(default="auto"),
    create_dirs: bool = Form(
        default=False,
        description="Create missing parent directories (mkdir -p) before upload",
    ),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    if create_dirs:
        ensure_directory_path(db, ws, path)
    body = await file.read()
    file_record, task_record = ingest_new_file(
        db,
        ws,
        ws.owner_id,
        parent_logical_path=path,
        upload_filename=file.filename or "unnamed",
        file_content=body,
        content_type=file.content_type,
        parser_type=parser_type,
        require_parent_dir=not create_dirs,
        duplicate_status_code=status.HTTP_409_CONFLICT,
    )
    return _upload_response_dict(file_record, task_record.id if task_record else None)
```

- [ ] **步骤 4:跑测试,确认变绿**

```bash
uv run pytest tests/test_service_api.py -k "create_dirs or without_create_dirs" -v
```

预期:3 passed。

- [ ] **步骤 5:跑全量收集用例(确认无回归)**

```bash
uv run pytest tests/test_service_api.py tests/test_files_api.py tests/test_workspace_file_tree.py -v
```

预期:全部 passed(含原有的 `test_service_upload_document_201`、`test_service_upload_duplicate_409`)。

- [ ] **步骤 6:提交**

```bash
git add openrag/src/openrag/api/service_api.py openrag/tests/test_service_api.py
git commit -m "feat(service-api): add create_dirs flag to service upload (O3)

Opt-in mkdir -p before upload via service token. Default false keeps
the strict 'Parent directory does not exist' behaviour for existing
callers; true materialises the parent path so the file is navigable.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### 任务 3(可选):建工作区时播种根 `/`

仅当你**还想**让 JWT/Web 创建的工作区一开始就带根行时才做。**修 O3 非必需**——任务 1 的 helper 已按需播种根。**回归风险:** 这会改变全新工作区的初始文件计数,做完务必跑全量收集用例。

**文件:**
- 改:`openrag/src/openrag/services/workspace_service.py`(`create_workspace`,第 15-46 行)
- 测试:`openrag/tests/test_service_api.py`

- [ ] **步骤 1:先写会失败的测试**

在 `openrag/tests/test_service_api.py` 末尾追加:

```python
def test_create_workspace_seeds_root_directory(db: Session, owner: User) -> None:
    from openrag.services.workspace_service import WorkspaceService

    ws = WorkspaceService(db).create_workspace(
        name="SeededWS", slug="seeded-ws", description=None, owner_id=owner.id
    )
    root = (
        db.query(File)
        .filter(File.workspace_id == ws.id, File.uri == "/", File.is_directory.is_(True))
        .first()
    )
    assert root is not None
```

- [ ] **步骤 2:跑测试,确认是红的**

```bash
uv run pytest tests/test_service_api.py -k seeds_root_directory -v
```

预期:FAIL —— `assert None is not None`。

- [ ] **步骤 3:实现根播种**

在 `openrag/src/openrag/services/workspace_service.py` 顶部加模型导入:

```python
from openrag.models.file import File as FileModel
```

在 `create_workspace` 里,把结尾(从 `self.db.commit()` 到 `return workspace`,第 43-46 行)替换为:

```python
        self.db.flush()  # 播种根前确保拿到 workspace.id

        root = FileModel(
            uri="/",
            name="root",
            owner_id=owner_id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
        self.db.add(root)
        self.db.commit()
        self.db.refresh(workspace)

        return workspace
```

- [ ] **步骤 4:跑该测试 + 全量收集用例**

```bash
uv run pytest tests/test_service_api.py -k seeds_root_directory -v
uv run pytest tests/test_service_api.py tests/test_files_api.py tests/test_workspace_file_tree.py -v
```

预期:目标用例通过;无回归。

- [ ] **步骤 5:提交**

```bash
git add openrag/src/openrag/services/workspace_service.py openrag/tests/test_service_api.py
git commit -m "feat(workspace): seed root directory row on workspace creation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### 任务 4:文档化 `create_dirs`

**文件:**
- 改:`docs/04-外部系统接入与API.md`(上传参数表 ~第 505 行;排障表 ~第 1188 行)

- [ ] **步骤 1:打开文件定位锚点**

在编辑器中阅读 `docs/04-外部系统接入与API.md` 第 497-511 行(上传参数表)和第 1180-1190 行(排障表)。

- [ ] **步骤 2:在上传参数表加一行 `create_dirs`**

紧挨着 `path` 那一行(含 `**父目录**逻辑路径；须已存在，不会自动创建缺失目录` 的行)之后插入:

```markdown
| `create_dirs` | bool | 否 | `false` | 为 `true` 时先自动创建 `path` 及其所有缺失父目录（`mkdir -p`）再上传；缺省 `false` 时父目录须已存在，否则 **400** |
```

- [ ] **步骤 3:更新排障表对应行**

把原有的 `Parent directory does not exist` 排障行(~第 1188 行)替换为:

```markdown
| 400 `Parent directory does not exist` | 上传时父目录未创建 | 上传时带 `create_dirs=true` 自动建目录；或先通过 Web 端 / JWT `POST /files/directories` 创建目录 |
```

- [ ] **步骤 4:提交**

```bash
git add docs/04-外部系统接入与API.md
git commit -m "docs(api): document create_dirs flag on service upload (O3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 六、自检(已完成)

**1. 需求覆盖:**
- 「建目录 helper(`mkdir -p`)」→ 任务 1。✓
- 「上传加 `create_dirs` 开关」→ 任务 2。✓
- 「根播种」→ 由任务 1 的 `ensure_directory_path` 兜底(按需播种 `/`);任务 3 为 JWT/Web 路径补主动播种(可选)。✓
- 「缺省行为不变」→ `test_service_upload_without_create_dirs_still_400`(任务 2)。✓
- 「非孤儿 / 可导航」→ `test_service_upload_create_dirs_file_is_navigable`(任务 2)。✓

**2. 占位符扫描:** 无 TBD/TODO;每个代码/测试步骤均给出完整内容;每个运行步骤均给出预期结果。✓

**3. 类型/命名一致性:** `ensure_directory_path(db, workspace, logical_path)` 在任务 1 定义、任务 2 同签名调用;`_get_or_create_directory_row` / `_ensure_directory_path_one_pass` 一致;`create_dirs`(snake_case)在端点与测试中一致。`File`/`FileModel`:测试用 `File`(测试模块已导入),生产代码用 `FileModel`(`file_ingest.py` 的别名),`DbFile` 未触碰。✓

**4. 已知边界:** `parent_id` 不写(已明确单独跟踪);`_file_summary` 的 JSON 形状只假设有 `name` 键(`item.get("name")` 容错)。✓

---

## 七、执行方式(请选)

1. **Subagent 驱动(推荐)** —— 每个任务派一个全新 subagent 实现,任务间我来 review,迭代快。
2. **本会话内联执行** —— 在当前会话按任务批量执行,带检查点。

另:**任务 3(建工作区播种根)默认做还是先跳过?** 建议先跳过——任务 1 的 helper 已覆盖 O3 的阻塞,任务 3 收益小、回归面大,可待主链合并后单独评估。
