# Service Token Document Retry Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让第三方通过现有 service-token 主链路拿到文档解析失败原因、当前可重试次数、总可重试次数，并能手动触发失败文档重试。

**Architecture:** 保持 OpenRAG 当前“上传后异步解析”的架构，不把上传接口改成同步等待解析结果。新增一个小的 service 层 helper 统一生成文档处理任务状态，避免 `upload`、`search-by-name`、`409` 和 `retry` 返回字段不一致。

**Tech Stack:** FastAPI service-token API、SQLAlchemy ORM、现有 `TaskService.retry_task`、pytest。

---

## 当前链路确认

GZClaw 当前不是通过 `documents/by-path` 确认上传和解析状态。

上传链路：

```text
GZClaw POST /api/knowledge-bases/{kb_id}/documents
-> provider.upload_document(...)
-> OpenRAG POST /service/v1/workspaces/{workspace_name}/documents
```

OpenRAG 返回 `id/path/task_id` 后，GZClaw 创建本地 Document，并先置为 `processing`。

后续状态同步链路：

```text
GET /api/knowledge-bases/documents/{doc_id}/status 或文档列表
-> _sync_processing_docs
-> provider.list_documents(...)
-> OpenRAG GET /service/v1/workspaces/{workspace_name}/documents/search-by-name?path_prefix=...
```

GZClaw 使用 `search-by-name` 返回项里的 `id/path/name` 等字段匹配本地 `provider_doc_id/storage_path/name`，再把本地状态更新为 `ready` 或 `failed`。

因此第一阶段必须优先增强 `search-by-name`，`documents/by-path` 只作为单文档补查、路径校验、预览链路的补充接口。

## 非目标

第一阶段不实现这些能力：

- 目录重命名。
- 目录移动。
- 目录删除。
- 文档移动。
- 文档重命名。
- 上传接口同步等待解析完成。

---

## 返回字段约定

所有涉及文档处理状态的 service-token 返回，统一补充以下字段。

```json
{
  "processing_status": "failed",
  "processing_error": "MilvusException...",
  "task_id": 6210,
  "task_uuid": "de060ad9-b68b-42ab-b387-e185a749648e",
  "task_status": "failure",
  "task_progress": 65,
  "retry_count": 1,
  "max_retries": 3,
  "remaining_retries": 2,
  "can_retry": true
}
```

字段语义：

- `processing_status`：文件表 `files.processing_status`，例如 `pending/parsing/building_hierarchy/embedding/completed/failed`。
- `processing_error`：文件表 `files.processing_error`。
- `task_id`：任务表自增主键 `tasks.id`，兼容当前上传返回已有字段。
- `task_uuid`：任务 UUID，对应 `tasks.task_id`。
- `task_status`：任务表 `tasks.status`。
- `task_progress`：任务表 `tasks.progress`。
- `retry_count`：已经消耗的重试次数，对应 `tasks.retry_count`。
- `max_retries`：总可重试次数，对应 `tasks.max_retries`。
- `remaining_retries`：剩余可重试次数，计算为 `max(max_retries - retry_count, 0)`。
- `can_retry`：只有最新 `process_document` 任务处于 `failure/cancelled` 且 `remaining_retries > 0` 时为 `true`。

没有处理任务的文档使用以下默认值：

```json
{
  "task_id": null,
  "task_uuid": null,
  "task_status": null,
  "task_progress": null,
  "retry_count": 0,
  "max_retries": 0,
  "remaining_retries": 0,
  "can_retry": false
}
```

---

## 执行步骤

### Step 1: 增加统一的文档任务状态 helper

**当前步骤解决的问题**

当前 `_upload_response_dict`、`_document_summary`、重复上传 409、未来 retry 接口各自组装返回。一旦分别加字段，很容易出现上传返回有 `max_retries`、`search-by-name` 没有，或者 `can_retry` 判断不一致的问题。

**解决方案**

新增一个 service 层模块，专门负责查询文件最新的 `process_document` 任务，并生成统一字段。API 层和文件上传 service 都复用它。

**创建文件**

- Create: `openrag/src/openrag/services/document_retry_status.py`
- Create: `openrag/tests/test_document_retry_status.py`

**创建函数**

在 `openrag/src/openrag/services/document_retry_status.py` 中创建：

```python
def latest_process_document_task(db: Session, file_id: int) -> Task | None:
    """Return the newest process_document task for a file."""
```

查询条件：

- `Task.file_id == file_id`
- `Task.task_type == "process_document"`
- 按 `Task.id.desc()` 取第一条。

```python
def task_retry_fields(task: Task | None) -> dict[str, Any]:
    """Return task id, UUID, status, progress, retry budget, and can_retry."""
```

计算规则：

- `remaining_retries = max(task.max_retries - task.retry_count, 0)`。
- `can_retry = task.status in (TaskStatus.FAILURE, TaskStatus.CANCELLED) and remaining_retries > 0`。
- `task is None` 时返回默认空任务字段。

```python
def document_processing_fields(db: Session, file: File) -> dict[str, Any]:
    """Return file processing state plus latest task retry fields."""
```

返回字段：

- `processing_status`
- `processing_error`
- `task_id`
- `task_uuid`
- `task_status`
- `task_progress`
- `retry_count`
- `max_retries`
- `remaining_retries`
- `can_retry`

```python
def document_conflict_detail(db: Session, file: File, file_uri: str) -> dict[str, Any]:
    """Return structured 409 detail for an existing file."""
```

返回规则：

- 如果 `file.processing_status == ProcessingStatus.failed`，`code` 为 `file_already_exists_processing_failed`。
- 其他状态，`code` 为 `file_already_exists`。
- 始终返回 `document`，用于让第三方定位冲突对象。

**新增字段**

此步骤不新增数据库字段，只新增 API 返回字段：

- `processing_error`
- `task_uuid`
- `task_status`
- `task_progress`
- `retry_count`
- `max_retries`
- `remaining_retries`
- `can_retry`

**测试 case**

在 `openrag/tests/test_document_retry_status.py` 新增：

```python
def test_task_retry_fields_failed_task_can_retry(...)
```

验证：

- `status=failure`
- `retry_count=1`
- `max_retries=3`
- 返回 `remaining_retries == 2`
- 返回 `can_retry is True`

```python
def test_task_retry_fields_exhausted_task_cannot_retry(...)
```

验证：

- `status=failure`
- `retry_count=3`
- `max_retries=3`
- 返回 `remaining_retries == 0`
- 返回 `can_retry is False`

```python
def test_document_processing_fields_uses_latest_process_document_task(...)
```

验证：

- 同一文件有两条 `process_document` 任务时，选择 `id` 最大的一条。
- 返回的 `task_id/task_uuid/retry_count` 来自最新任务。

```python
def test_document_conflict_detail_failed_file_is_retryable(...)
```

验证：

- 文件 `processing_status=failed`
- 最新任务 `status=failure`
- 返回 `code == "file_already_exists_processing_failed"`
- 返回 `document.can_retry is True`

**验证命令**

```bash
pytest openrag/tests/test_document_retry_status.py -q
```

期望：

```text
4 passed
```

---

### Step 2: 上传成功返回初始任务额度

**当前步骤解决的问题**

GZClaw 上传后只拿到 `id/path/task_id`，只能知道 OpenRAG 接收了文件，不能知道这个新任务的总重试额度。虽然上传返回不能代表解析成功，但应该告诉调用方这个异步任务的初始重试预算。

**解决方案**

修改 service-token 上传返回 helper，让它接收 `Task` 对象，而不是只接收 `task.id`。这样可以直接返回 `task_uuid/retry_count/max_retries/remaining_retries/can_retry`。

**修改文件**

- Modify: `openrag/src/openrag/api/service_api.py`
- Test: `openrag/tests/test_service_api.py`
- Test: `openrag/tests/test_service_api_upsert.py`

**修改函数**

在 `openrag/src/openrag/api/service_api.py` 中修改：

```python
def _upload_response_dict(file_record: DbFile, task_id: int | None) -> dict[str, Any]:
```

调整为：

```python
def _upload_response_dict(db: Session, file_record: DbFile, task_record: Task | None) -> dict[str, Any]:
```

返回体保留现有字段，并合并：

```python
document_processing_fields(db, file_record)
```

同步修改调用点：

- `service_upload_document`
- `service_replace_document`
- `service_upsert_document_by_tag`

调用方式从：

```python
return _upload_response_dict(file_record, task_record.id if task_record else None)
```

改为：

```python
return _upload_response_dict(db, file_record, task_record)
```

**新增字段**

上传、替换、upsert 成功返回新增：

- `processing_status`
- `processing_error`
- `task_uuid`
- `task_status`
- `task_progress`
- `retry_count`
- `max_retries`
- `remaining_retries`
- `can_retry`

保留已有 `task_id` 字段语义：仍返回 `tasks.id`。

**测试 case**

更新 `openrag/tests/test_service_api.py`：

```python
def test_service_upload_document_201(...)
```

新增断言：

```python
assert body["processing_status"] == "pending"
assert body["task_id"] is not None
assert body["task_uuid"] is not None
assert body["task_status"] == "pending"
assert body["task_progress"] == 0
assert body["retry_count"] == 0
assert body["max_retries"] == 3
assert body["remaining_retries"] == 3
assert body["can_retry"] is False
```

更新：

```python
def test_service_replace_document_200(...)
```

新增同类断言，确认替换文件后新任务的 retry budget 返回正确。

更新 `openrag/tests/test_service_api_upsert.py` 中创建和更新分支测试，确认 `upsert-by-tag` 返回同样字段。

**验证命令**

```bash
pytest openrag/tests/test_service_api.py::test_service_upload_document_201 openrag/tests/test_service_api.py::test_service_replace_document_200 openrag/tests/test_service_api_upsert.py -q
```

期望：

```text
passed
```

---

### Step 3: `search-by-name` 列表项透传当前失败原因和重试额度

**当前步骤解决的问题**

GZClaw 后续状态同步主链路是 `provider.list_documents(...) -> search-by-name`。如果只在上传返回或 `by-path` 返回里加字段，用户第一次重试后再次解析失败时，列表同步仍然拿不到 `remaining_retries=2` 这类实时额度。

**解决方案**

增强 `search-by-name` 使用的 `_document_summary`，每个 `items[]` 都返回当前文件的处理状态、失败原因和最新任务重试额度。

**修改文件**

- Modify: `openrag/src/openrag/api/service_api.py`
- Test: `openrag/tests/test_service_api.py`

**修改函数**

在 `openrag/src/openrag/api/service_api.py` 中修改：

```python
def _document_summary(f: DbFile) -> dict[str, Any]:
```

调整为：

```python
def _document_summary(db: Session, f: DbFile) -> dict[str, Any]:
```

保留现有字段：

- `id`
- `path`
- `name`
- `size`
- `mime_type`
- `tag`
- `processing_status`
- `updated_at`

合并：

```python
document_processing_fields(db, f)
```

同步修改调用点：

- `service_search_documents_by_name`
- `service_document_by_tag`

建议同步补齐但非主链路：

- `service_document_by_path`

**新增字段**

`search-by-name` 的每个 `items[]` 新增：

- `processing_error`
- `task_id`
- `task_uuid`
- `task_status`
- `task_progress`
- `retry_count`
- `max_retries`
- `remaining_retries`
- `can_retry`

**测试 case**

在 `openrag/tests/test_service_api.py` 新增：

```python
def test_service_search_by_name_includes_retry_budget_for_failed_document(...)
```

测试数据：

- 创建 `/docs/fail.txt`
- 设置 `file.processing_status = ProcessingStatus.failed`
- 设置 `file.processing_error = "milvus timeout"`
- 创建关联任务：
  - `task_type="process_document"`
  - `status=TaskStatus.FAILURE`
  - `progress=65`
  - `retry_count=1`
  - `max_retries=3`

请求：

```python
r = client.get(
    f"/service/v1/workspaces/{workspace.name}/documents/search-by-name",
    params={"path_prefix": "/docs"},
    headers=service_token_headers,
)
```

断言：

```python
item = r.json()["items"][0]
assert item["processing_status"] == "failed"
assert item["processing_error"] == "milvus timeout"
assert item["task_status"] == "failure"
assert item["task_progress"] == 65
assert item["retry_count"] == 1
assert item["max_retries"] == 3
assert item["remaining_retries"] == 2
assert item["can_retry"] is True
```

再新增：

```python
def test_service_search_by_name_exhausted_retry_budget_cannot_retry(...)
```

断言：

- `retry_count == 3`
- `max_retries == 3`
- `remaining_retries == 0`
- `can_retry is False`

**验证命令**

```bash
pytest openrag/tests/test_service_api.py::test_service_search_by_name_includes_retry_budget_for_failed_document openrag/tests/test_service_api.py::test_service_search_by_name_exhausted_retry_budget_cannot_retry -q
```

期望：

```text
2 passed
```

---

### Step 4: 同路径重复上传返回结构化 409

**当前步骤解决的问题**

用户遇到的核心问题是：OpenRAG 文件列表里看不到失败文档，但重新上传同路径文件仍返回 `File already exists`。当前 409 返回只有字符串，第三方无法判断这个冲突文件是不是失败态，也不知道是否可以直接调用 retry。

**解决方案**

保留 409 语义，不允许同路径重复上传直接覆盖已有文件。但当已有文件存在时，返回结构化 detail，包含冲突文档摘要和 retry budget。

**修改文件**

- Modify: `openrag/src/openrag/services/file_ingest.py`
- Test: `openrag/tests/test_service_api.py`

**修改函数**

在 `openrag/src/openrag/services/file_ingest.py` 中修改：

```python
def ingest_new_file(...)
```

处理 `existing_file` 分支时：

当前：

```python
raise HTTPException(
    status_code=duplicate_status_code,
    detail=f"File already exists at {file_uri}",
)
```

调整规则：

- 当 `duplicate_status_code == status.HTTP_409_CONFLICT` 时，使用 `document_conflict_detail(db, existing_file, file_uri)` 作为 `detail`。
- 当 `duplicate_status_code` 不是 409 时，保持当前字符串 detail，避免影响 JWT `/files/upload` 既有行为。

同时处理唯一约束 race 分支：

- 捕获 `IntegrityError` 且 `kind == "uri"` 后，如果是 409，重新查询同 workspace + uri 的 existing file。
- 查到 existing file 时返回同样结构化 detail。
- 查不到时保持现有字符串 detail。

**新增字段**

409 detail 新增结构：

```json
{
  "code": "file_already_exists_processing_failed",
  "message": "File already exists and previous processing failed",
  "document": {
    "id": 7383,
    "path": "/dir/file.md",
    "name": "file.md",
    "processing_status": "failed",
    "processing_error": "milvus timeout",
    "task_id": 6210,
    "task_uuid": "...",
    "task_status": "failure",
    "task_progress": 65,
    "retry_count": 0,
    "max_retries": 3,
    "remaining_retries": 3,
    "can_retry": true
  }
}
```

非失败态冲突返回：

```json
{
  "code": "file_already_exists",
  "message": "File already exists at /dir/file.md",
  "document": {
    "processing_status": "completed",
    "can_retry": false
  }
}
```

**测试 case**

更新或新增 `openrag/tests/test_service_api.py`：

```python
def test_service_upload_duplicate_failed_file_409_returns_retry_document(...)
```

测试数据：

- 已有 `/in/dup.txt`
- `processing_status=failed`
- `processing_error="milvus timeout"`
- 最新任务 `status=failure`
- `retry_count=0`
- `max_retries=3`

请求同路径上传。

断言：

```python
assert r.status_code == 409
detail = r.json()["detail"]
assert detail["code"] == "file_already_exists_processing_failed"
assert detail["document"]["path"] == "/in/dup.txt"
assert detail["document"]["processing_status"] == "failed"
assert detail["document"]["remaining_retries"] == 3
assert detail["document"]["can_retry"] is True
```

新增：

```python
def test_service_upload_duplicate_completed_file_409_returns_non_retryable_document(...)
```

断言：

```python
assert detail["code"] == "file_already_exists"
assert detail["document"]["processing_status"] == "completed"
assert detail["document"]["can_retry"] is False
```

保留现有：

```python
def test_service_upload_duplicate_409(...)
```

可以只断言 `409`，避免过度耦合。

**验证命令**

```bash
pytest openrag/tests/test_service_api.py::test_service_upload_duplicate_409 openrag/tests/test_service_api.py::test_service_upload_duplicate_failed_file_409_returns_retry_document openrag/tests/test_service_api.py::test_service_upload_duplicate_completed_file_409_returns_non_retryable_document -q
```

期望：

```text
3 passed
```

---

### Step 5: 新增 service-token 手动重试接口

**当前步骤解决的问题**

OpenRAG 已有 `TaskService.retry_task(task.id)`，但只暴露在 JWT 任务接口里。第三方 service-token 调用方无法基于失败文档手动触发重试。

**解决方案**

新增 service-token 文档 retry API。优先支持按 OpenRAG 文档 id 重试，因为 GZClaw 上传后已经拿到 OpenRAG 返回的 `id`，通常会保存为本地 `provider_doc_id`。同时增加 by-path retry，覆盖只知道路径的排障和兼容场景。

**修改文件**

- Modify: `openrag/src/openrag/api/service_api.py`
- Modify: `openrag/src/openrag/services/document_retry_status.py`
- Test: `openrag/tests/test_service_api.py`

**创建函数**

在 `openrag/src/openrag/services/document_retry_status.py` 中创建：

```python
def retry_failed_document_processing(db: Session, file: File) -> Task:
    """Retry latest failed/cancelled process_document task for a file."""
```

行为：

1. 调用 `latest_process_document_task(db, file.id)`。
2. 如果没有任务，抛出 `HTTPException(409, detail="No process_document task to retry")`。
3. 如果任务不是 `failure/cancelled`，抛出 `HTTPException(409, detail="Document is not in a retryable state")`。
4. 如果 `retry_count >= max_retries`，抛出 `HTTPException(409, detail="Retry limit reached")`。
5. 调用 `TaskService(db).retry_task(task.id)`。
6. 将 `file.processing_status` 设置为 `ProcessingStatus.pending`。
7. 将 `file.processing_error` 设置为 `None`。
8. 提交并返回 retry 后的任务。

在 `openrag/src/openrag/api/service_api.py` 中创建内部 helper：

```python
def _get_service_document_by_id(db: Session, workspace_id: int, document_id: int) -> DbFile:
```

查询条件：

- `DbFile.id == document_id`
- `DbFile.workspace_id == workspace_id`
- `DbFile.is_directory.is_(False)`
- `DbFile.deleted_at.is_(None)`

没找到时返回 404。

新增路由：

```python
@router.post("/workspaces/{workspace_name}/documents/{document_id}/retry")
async def service_retry_document_by_id(...):
```

权限：

- `assert_token_workspace_permission(ctx, ws.id, "write")`

返回：

```python
return _document_summary(db, file_record)
```

新增 by-path 路由：

```python
@router.post("/workspaces/{workspace_name}/documents/by-path/retry")
async def service_retry_document_by_path(...):
```

路径解析复用：

```python
p = validate_path(path)
```

再按 workspace + uri 查询文件。

**新增字段**

retry API 返回和 `search-by-name` 单项保持一致：

- `processing_status`
- `processing_error`
- `task_id`
- `task_uuid`
- `task_status`
- `task_progress`
- `retry_count`
- `max_retries`
- `remaining_retries`
- `can_retry`

**测试 case**

在 `openrag/tests/test_service_api.py` 新增：

```python
def test_service_retry_document_by_id_resets_file_and_requeues_task(...)
```

测试数据：

- 文件 `processing_status=failed`
- `processing_error="milvus timeout"`
- 最新任务 `status=failure`
- `retry_count=0`
- `max_retries=3`

请求：

```python
r = client.post(
    f"/service/v1/workspaces/{workspace.name}/documents/{file.id}/retry",
    headers=service_token_write_headers,
)
```

断言：

```python
assert r.status_code == 200
body = r.json()
assert body["processing_status"] == "pending"
assert body["processing_error"] is None
assert body["task_status"] == "pending"
assert body["retry_count"] == 1
assert body["remaining_retries"] == 2
assert body["can_retry"] is False
```

并查询数据库确认：

```python
db.refresh(file)
db.refresh(task)
assert file.processing_status == ProcessingStatus.pending
assert file.processing_error is None
assert task.status == TaskStatus.PENDING
assert task.retry_count == 1
```

新增：

```python
def test_service_retry_document_by_id_exhausted_returns_409(...)
```

断言：

- `retry_count=3`
- `max_retries=3`
- API 返回 409
- 文件状态仍是 `failed`

新增：

```python
def test_service_retry_document_by_id_requires_write_permission(...)
```

用 read-only service token 调用，断言 403。

新增：

```python
def test_service_retry_document_by_path_resets_file_and_requeues_task(...)
```

用 `params={"path": "/docs/fail.txt"}` 调用 by-path retry，断言和 by-id 一致。

**验证命令**

```bash
pytest openrag/tests/test_service_api.py::test_service_retry_document_by_id_resets_file_and_requeues_task openrag/tests/test_service_api.py::test_service_retry_document_by_id_exhausted_returns_409 openrag/tests/test_service_api.py::test_service_retry_document_by_id_requires_write_permission openrag/tests/test_service_api.py::test_service_retry_document_by_path_resets_file_and_requeues_task -q
```

期望：

```text
4 passed
```

---

### Step 6: 保持 `documents/by-path` 和 `documents/by-tag` 字段一致

**当前步骤解决的问题**

虽然 GZClaw 主链路不是 `by-path`，但 OpenRagProvider 的 `_get_document_by_path()`、搜索中的 document path 校验、预览链路会使用这些单文档查询。如果这些接口字段比 `search-by-name` 少，排障时会出现“列表里能看到错误和额度，单文档查询看不到”的不一致。

**解决方案**

复用 Step 3 的 `_document_summary(db, f)`，让 `by-path` 和 `by-tag` 返回字段与 `search-by-name` 单项一致。

**修改文件**

- Modify: `openrag/src/openrag/api/service_api.py`
- Test: `openrag/tests/test_service_api.py`
- Test: `openrag/tests/test_service_api_tag.py`

**修改函数**

修改：

```python
async def service_document_by_path(...)
```

从手写 dict 改为：

```python
return _document_summary(db, f)
```

确认：

```python
async def service_document_by_tag(...)
```

也返回：

```python
return _document_summary(db, f)
```

**新增字段**

同 Step 3。

**测试 case**

在 `openrag/tests/test_service_api.py` 新增：

```python
def test_service_document_by_path_includes_retry_budget(...)
```

断言 `by-path` 返回：

- `processing_error`
- `task_uuid`
- `remaining_retries`
- `can_retry`

在 `openrag/tests/test_service_api_tag.py` 新增：

```python
def test_service_document_by_tag_includes_retry_budget(...)
```

断言同上。

**验证命令**

```bash
pytest openrag/tests/test_service_api.py::test_service_document_by_path_includes_retry_budget openrag/tests/test_service_api_tag.py::test_service_document_by_tag_includes_retry_budget -q
```

期望：

```text
2 passed
```

---

### Step 7: 回归测试和兼容性检查

**当前步骤解决的问题**

本次改动涉及 service-token 上传、列表、重复上传、retry 和单文档查询。需要确认没有破坏已有 service-token 行为，也没有误改 JWT `/files/upload` 的重复上传错误格式。

**解决方案**

跑 service-token 相关测试和任务服务测试。重点验证：

- 现有上传仍返回 201。
- 现有重复上传仍返回 409。
- `search-by-name` 分页和 path_prefix 行为不变。
- read-only token 仍不能写。
- `TaskService.retry_task` 既有语义不变。
- JWT `/files/upload` 的 duplicate detail 不被本次 service-token 结构化 409 影响。

**修改文件**

此步骤不修改代码。

**创建函数**

无。

**新增字段**

无。

**测试 case**

运行已有测试加新增测试：

```bash
pytest openrag/tests/test_document_retry_status.py openrag/tests/test_service_api.py openrag/tests/test_service_api_tag.py openrag/tests/test_service_api_upsert.py openrag/tests/test_task_management.py openrag/tests/test_files_api.py -q
```

如果时间紧，至少运行第一阶段核心切片：

```bash
pytest openrag/tests/test_document_retry_status.py openrag/tests/test_service_api.py openrag/tests/test_service_api_tag.py openrag/tests/test_service_api_upsert.py -q
```

期望：

```text
passed
```

**人工验证场景**

1. 上传新文档，OpenRAG 返回 `processing_status=pending`、`max_retries=3`、`remaining_retries=3`。
2. 模拟 worker 将任务置为 failure、文件置为 failed。
3. 调用 `search-by-name`，确认返回 `can_retry=true`。
4. 调用 `POST /service/v1/workspaces/{workspace_name}/documents/{document_id}/retry`。
5. 再调用 `search-by-name`，确认 `processing_status=pending`、`retry_count=1`、`remaining_retries=2`。
6. 再次模拟失败后调用 `search-by-name`，确认 `can_retry=true` 且剩余 2 次。

---

## 最终验收标准

- [ ] 上传成功返回包含 `task_uuid/retry_count/max_retries/remaining_retries/can_retry`。
- [ ] `search-by-name` 每个 `items[]` 包含当前处理状态、失败原因和重试额度。
- [ ] 失败文档重复上传时，409 返回结构化 `document` 信息。
- [ ] 已完成或处理中状态的重复上传仍返回 409，但 `document.can_retry=false`。
- [ ] service-token 可按 `document_id` 手动触发失败文档重试。
- [ ] service-token 可按 `path` 手动触发失败文档重试。
- [ ] retry 后文件状态回到 `pending`，`processing_error` 清空，任务 `retry_count` 增加。
- [ ] 重试次数耗尽后 `remaining_retries=0` 且 `can_retry=false`，retry API 返回 409。
- [ ] 现有 JWT `/files/upload` 重复上传行为不因 service-token 结构化 409 被意外改变。
