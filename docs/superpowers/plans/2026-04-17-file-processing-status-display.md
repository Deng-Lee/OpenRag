# File Processing Status Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐文件处理状态全链路：数据库 `processing_error`、Worker 失败写入、`FileResponse` 输出 `processing_status` / `simple_status` / `error_message`、成功路径清空错误、前端文件列表展示四态 Tag 与失败 Popover（含重新处理入口）。

**Architecture:** 保留 `ProcessingStatus` 六枚举不变；`simple_status` 仅在 API 层由映射表派生；错误文本仅存 `files.processing_error`（截断 4096）；`TaskWorker` 在 `process_document` 任务最终失败时双写 `failed` + error；`DocumentProcessor` 在 `completed` 时清空 `processing_error`，与 `cleanup_file_processing_data` 的重置一致。

**Tech Stack:** FastAPI + Pydantic v2、SQLAlchemy 2.0、PostgreSQL（生产）/ SQLite（pytest）、React 18 + antd 5 + i18next、Vitest。

---

## File map（本功能涉及）

| 文件 | 职责 |
|---|---|
| `openrag/src/openrag/models/file.py` | 新增 `processing_error` 列；`validates` 纳入 NUL 剥离 |
| `openrag/src/openrag/api/files_api.py` | `SIMPLE_STATUS_MAP`、`_file_to_response` / `_file_to_upload_response`、替换所有手写 `FileResponse`/`FileUploadResponse` 构造 |
| `openrag/src/openrag/services/file_ingest.py` | 若创建 `File` 时需显式设 `processing_error=None`（通常可省略，默认 NULL） |
| `openrag/src/openrag/processors/document_processor.py` | 成功落库 `completed` 时 `processing_error = None` |
| `openrag/src/openrag/worker/task_worker.py` | `_mark_file_failed` + `_execute_task` except 分支调用 |
| `CURRENT_MODEL_SCHEMA_DDL.md` | `files` 表增加 `processing_error` 说明与示例 DDL |
| `web/src/types/index.ts` | `File` 接口扩展三个可选字段 |
| `web/src/components/FileList.tsx` | 状态列 + Tag + Popover + 复用 `handleReprocess` |
| `web/src/i18n/locales/zh.json` / `en.json` | 列标题与四态 + `failed_title` / `no_error` |
| `openrag/tests/test_files_api.py` | 列表/详情断言新字段；目录行为 |
| `web/src/components/FileList.test.tsx` | 四态与失败 Popover、目录 `-` |

**数据库迁移说明：** 当前仓库根目录**未发现** `alembic.ini`。生产 PostgreSQL 需执行：

```sql
ALTER TABLE files ADD COLUMN IF NOT EXISTS processing_error TEXT;
```

若团队后续引入 Alembic，用等价 revision 替代上述手工 SQL。

---

### Task 1: 模型 + DDL 文档

**Files:**
- Modify: `openrag/src/openrag/models/file.py`
- Modify: `CURRENT_MODEL_SCHEMA_DDL.md`（`files` 表片段与 §2 脚本）

- [ ] **Step 1: 在 `File` 模型增加 `processing_error`**

在 `openrag/src/openrag/models/file.py` 中：

1. 增加 import：`from sqlalchemy import ... Text`（若尚无 `Text`）。
2. 在 `processing_status` 字段**之后**增加：

```python
    processing_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Last processing failure message (truncated when stored from worker)",
    )
```

3. 把 `"processing_error"` 加入 `@validates(...)` 装饰器元组，与 `processing_status` 同属 NUL 剥离字段。

- [ ] **Step 2: 更新 DDL 快照文档**

在 `CURRENT_MODEL_SCHEMA_DDL.md` 的 `CREATE TABLE files` 片段中，在 `processing_status` 行后增加：

```sql
    processing_error TEXT NULL,
```

并在文首「表清单」或说明段注明该列为新增可空列。

- [ ] **Step 3: 本地/生产执行 SQL（由部署者执行）**

PostgreSQL：

```sql
ALTER TABLE files ADD COLUMN IF NOT EXISTS processing_error TEXT;
```

- [ ] **Step 4: Commit**

```bash
git add openrag/src/openrag/models/file.py CURRENT_MODEL_SCHEMA_DDL.md
git commit -m "feat(models): add files.processing_error for pipeline failures"
```

---

### Task 2: API 响应层 — 映射与统一构造

**Files:**
- Modify: `openrag/src/openrag/api/files_api.py`（约 126–316、394–409、570–581、852–863、948–959、1044–1056）

- [ ] **Step 1: 在 `FileResponse` 增加字段**

在 `class FileResponse(BaseModel):` 中，在 `updated_at` 之后增加（保持 `model_config`）：

```python
    processing_status: Optional[str] = None
    simple_status: Optional[str] = None
    error_message: Optional[str] = None
```

`FileUploadResponse` 继承 `FileResponse`，无需重复声明；确保 `task_id` 仍在子类中。

- [ ] **Step 2: 增加常量与两个 helper**

在 `FileResponse` 定义**上方**（模块级）：

```python
_SIMPLE_STATUS_MAP: dict[str, str] = {
    "pending": "unprocessed",
    "parsing": "processing",
    "building_hierarchy": "processing",
    "embedding": "processing",
    "completed": "done",
    "failed": "failed",
}

def _processing_status_str(file: FileModel) -> Optional[str]:
    if file.is_directory:
        return None
    ps = file.processing_status
    if ps is None:
        return None
    return ps.value if hasattr(ps, "value") else str(ps)

def _file_to_response(file: FileModel) -> FileResponse:
    ps = _processing_status_str(file)
    ss = _SIMPLE_STATUS_MAP.get(ps) if ps else None
    err: Optional[str] = None
    if ss == "failed":
        err = file.processing_error
    return FileResponse(
        id=file.id,
        uri=file.uri,
        name=file.name,
        owner_id=file.owner_id,
        parent_id=file.parent_id,
        is_directory=file.is_directory,
        size=file.size,
        mime_type=file.mime_type,
        created_at=file.created_at.isoformat(),
        updated_at=file.updated_at.isoformat(),
        processing_status=ps,
        simple_status=ss,
        error_message=err,
    )

def _file_to_upload_response(file: FileModel, task_id: Optional[int]) -> FileUploadResponse:
    base = _file_to_response(file)
    return FileUploadResponse(
        **base.model_dump(),
        task_id=task_id,
    )
```

若 Pydantic 版本对 `model_dump` 行为有差异，可改为显式传参复制 12 个基础字段 + 三个新字段 + `task_id`。

- [ ] **Step 3: 替换所有 `FileResponse(` / `FileUploadResponse(` 手写构造**

必须替换的端点（当前代码位置供检索）：

| 端点 | 替换为 |
|---|---|
| `upload_file` 返回 | `_file_to_upload_response(file_record, task_record.id if task_record else None)` |
| `list_files` 中 `items = [...]` | `items = [_file_to_response(f) for f in paginated_files]` |
| `get_file` | `return _file_to_response(file)` |
| `move_file` | `return _file_to_response(file)` |
| `create_directory` | `return _file_to_response(directory)` |
| `reprocess_file` | `return _file_to_upload_response(file, task_record.id)` |

删除重复的字段逐一赋值块。

- [ ] **Step 4: `cleanup_file_processing_data` 清空错误**

在 `file.processing_status = ProcessingStatus.pending` 附近增加：

```python
    file.processing_error = None
```

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/api/files_api.py
git commit -m "feat(api): expose processing status and simple_status on file responses"
```

---

### Task 3: DocumentProcessor 成功时清空 `processing_error`

**Files:**
- Modify: `openrag/src/openrag/processors/document_processor.py`（Step 7 更新元数据处，约 294–300）

- [ ] **Step 1: 在 `completed` 分支清空**

在 `file_record.processing_status = ProcessingStatus.completed` **同一事务块**中增加：

```python
        file_record.processing_error = None
```

- [ ] **Step 2: Commit**

```bash
git add openrag/src/openrag/processors/document_processor.py
git commit -m "fix(pipeline): clear processing_error on successful completion"
```

---

### Task 4: Worker — `_mark_file_failed`

**Files:**
- Modify: `openrag/src/openrag/worker/task_worker.py`（`TaskWorker` 类内，`_execute_task`）

- [ ] **Step 1: 增加模块常量与私有方法**

在 `TaskWorker` 类内、`__init__` 之后合适位置：

```python
    _MAX_PROCESSING_ERROR_LEN = 4096

    def _mark_file_failed(self, file_id: int, error: str) -> None:
        from openrag.models.file import File, ProcessingStatus

        msg = (error or "")[: self._MAX_PROCESSING_ERROR_LEN]
        db = SessionLocal()
        try:
            row = db.query(File).filter(File.id == file_id).first()
            if not row or row.is_directory:
                return
            row.processing_status = ProcessingStatus.failed
            row.processing_error = msg or None
            db.commit()
        finally:
            db.close()
```

- [ ] **Step 2: 修改 `_execute_task` 的 `except` 块**

将：

```python
        except Exception as e:
            # Report failure
            self._update_task_status(
                task_id,
                "failure",
                error=str(e)
            )
```

改为（保留原有 `_update_task_status` 调用顺序：先尝试标文件失败，再更新 task）：

```python
        except Exception as e:
            task_type = task.get("task_type") or "process_document"
            file_id = task.get("file_id")
            if task_type == "process_document" and file_id:
                try:
                    self._mark_file_failed(int(file_id), str(e))
                except Exception as mark_exc:
                    _logger.warning(
                        "Failed to mark file %s as failed: %s", file_id, mark_exc
                    )
            self._update_task_status(
                task_id,
                "failure",
                error=str(e),
            )
```

- [ ] **Step 3: Commit**

```bash
git add openrag/src/openrag/worker/task_worker.py
git commit -m "feat(worker): mark file failed and store processing_error on task failure"
```

---

### Task 5: 后端测试 `test_files_api.py`

**Files:**
- Modify: `openrag/tests/test_files_api.py`

- [ ] **Step 1: 扩展 fixture / 新用例**

1. `from openrag.models.file import ProcessingStatus`（若尚无）。
2. 新增测试类 `TestFileProcessingStatus`（或并入 `TestFileList`）：

```python
def test_list_files_includes_simple_status_and_null_for_directory(
    self, client, db, test_user, test_file, test_directory
):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    test_file.processing_status = ProcessingStatus.embedding
    db.add(test_file)
    db.commit()

    response = client.get("/files/")
    assert response.status_code == status.HTTP_200_OK
    items = {row["id"]: row for row in response.json()["items"]}
    assert items[test_file.id]["processing_status"] == "embedding"
    assert items[test_file.id]["simple_status"] == "processing"
    assert items[test_file.id].get("error_message") in (None, "")
    assert items[test_directory.id]["processing_status"] is None
    assert items[test_directory.id]["simple_status"] is None


def test_get_file_failed_includes_error_message(
    self, client, db, test_user, test_file
):
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
    test_file.processing_status = ProcessingStatus.failed
    test_file.processing_error = "boom"
    db.add(test_file)
    db.commit()

    response = client.get(f"/files/{test_file.id}")
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["simple_status"] == "failed"
    assert body["error_message"] == "boom"
```

3. 若 SQLite 对 `TEXT` 列需迁移：`Base.metadata.create_all` 会按**当前**模型建表，运行前确保 Task 1 已合并。

- [ ] **Step 2: 运行测试**

```bash
cd OpenRag && pytest tests/test_files_api.py::TestFileProcessingStatus -v
```

预期：全部 PASS。

- [ ] **Step 3: 全量 files 相关回归**

```bash
cd OpenRag && pytest tests/test_files_api.py -v
```

- [ ] **Step 4: Commit**

```bash
git add openrag/tests/test_files_api.py
git commit -m "test(api): cover file simple_status and failed error_message"
```

---

### Task 6: 前端类型 + i18n

**Files:**
- Modify: `web/src/types/index.ts`
- Modify: `web/src/i18n/locales/zh.json`
- Modify: `web/src/i18n/locales/en.json`

- [ ] **Step 1: 扩展 `File` 接口**

在 `web/src/types/index.ts` 的 `export interface File` 内 `updated_at` 后增加：

```typescript
  processing_status?: string | null;
  simple_status?: 'unprocessed' | 'processing' | 'done' | 'failed' | null;
  error_message?: string | null;
```

- [ ] **Step 2: i18n 键**

在 `files.columns` 下增加 `"status": "状态"` / `"Status"`。

在 `files.status` 下增加：

```json
"unprocessed": "未处理",
"processing": "处理中",
"done": "已处理",
"failed": "处理失败",
"failed_title": "处理失败",
"no_error": "没有错误详情",
"reprocess_from_popover": "重新处理"
```

英文对应：`Unprocessed`, `Processing`, `Done`, `Failed`, `Processing failed`, `No error detail`, `Reprocess`.

- [ ] **Step 3: Commit**

```bash
git add web/src/types/index.ts web/src/i18n/locales/zh.json web/src/i18n/locales/en.json
git commit -m "feat(web): types and i18n for file processing status"
```

---

### Task 7: `FileList.tsx` 状态列 + Popover

**Files:**
- Modify: `web/src/components/FileList.tsx`

- [ ] **Step 1: import**

增加：`Tag`, `Popover` from `antd`（与现有 import 合并为一行）。

- [ ] **Step 2: 增加渲染函数（组件内、`columns` 之前）**

```typescript
type SimpleKey = 'unprocessed' | 'processing' | 'done' | 'failed';

const STATUS_ORDER: Record<SimpleKey, { color: string; labelKey: string }> = {
  unprocessed: { color: 'default', labelKey: 'files.status.unprocessed' },
  processing: { color: 'processing', labelKey: 'files.status.processing' },
  done: { color: 'success', labelKey: 'files.status.done' },
  failed: { color: 'error', labelKey: 'files.status.failed' },
};

function renderStatusCell(record: File, onReprocess: (f: File) => void, t: TFunction) {
  if (record.is_directory) return '-';
  const key = record.simple_status as SimpleKey | null | undefined;
  if (!key || !STATUS_ORDER[key]) return '-';
  const cfg = STATUS_ORDER[key];
  const label = t(cfg.labelKey);
  const tag = <Tag color={cfg.color}>{label}</Tag>;
  if (key !== 'failed') return tag;
  const body = (
    <div style={{ maxWidth: 360 }}>
      <pre
        style={{
          whiteSpace: 'pre-wrap',
          maxHeight: 240,
          overflow: 'auto',
          margin: 0,
          fontSize: 12,
        }}
      >
        {record.error_message?.trim() ? record.error_message : t('files.status.no_error')}
      </pre>
      <Button type="link" size="small" icon={<ReloadOutlined />} onClick={() => onReprocess(record)}>
        {t('files.status.reprocess_from_popover')}
      </Button>
    </div>
  );
  return (
    <Popover title={t('files.status.failed_title')} content={body} trigger="click">
      {tag}
    </Popover>
  );
}
```

将 `TFunction` 改为项目实际类型（通常 `import type { TFunction } from 'i18next'`）或直接用 `ReturnType<typeof useTranslation>['t']`。

- [ ] **Step 3: 在 `columns` 数组中「上传时间」列之前插入「状态」列**

```typescript
    {
      title: t('files.columns.status'),
      key: 'status',
      width: 120,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (_: unknown, record: File) =>
        renderStatusCell(record, handleReprocess, t),
    },
```

确保 `handleReprocess` 在 `columns` 定义之前已声明（若顺序冲突，把 `columns` 改为 `useMemo` 依赖 `[t, canWrite, ...]`，或把 `renderStatusCell` 内联到 `useMemo`）。

- [ ] **Step 4: Commit**

```bash
git add web/src/components/FileList.tsx
git commit -m "feat(web): show file processing status with failed detail popover"
```

---

### Task 8: 前端测试 `FileList.test.tsx`

**Files:**
- Modify: `web/src/components/FileList.test.tsx`

- [ ] **Step 1: 增加用例**

使用 `@testing-library/user-event` 若已安装；否则 `fireEvent.click`。覆盖：

1. `simple_status: 'done'` → 可见「已处理」或英文（按测试环境 `i18n` 默认语言）。
2. `is_directory: true` → 状态列 `-`。
3. `simple_status: 'failed'` + `error_message: 'x'` → 点击 Tag 后可见 `x`。

示例骨架：

```typescript
import userEvent from '@testing-library/user-event';

it('shows status tag for done', async () => {
  const files = [{
    id: 1,
    uri: '/a.pdf',
    name: 'a.pdf',
    owner_id: 1,
    is_directory: false,
    size: 1,
    mime_type: 'application/pdf',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    simple_status: 'done',
  }];
  render(<FileList files={files} onFileDeleted={vi.fn()} />);
  expect(screen.getByText(/已处理|Done/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: 运行**

```bash
cd web && npm run test -- --run src/components/FileList.test.tsx
```

- [ ] **Step 3: Commit**

```bash
git add web/src/components/FileList.test.tsx
git commit -m "test(web): FileList processing status and failed popover"
```

---

### Task 9: 文档与 spec 对齐（可选小修）

**Files:**
- Modify: `docs/superpowers/specs/2026-04-17-file-processing-status-display-design.md` 将 **Status** 改为 `Approved`（若团队流程要求）

- [ ] **Step 1: 若需同步 `docs/04-外部系统接入与API.md` 中 File 响应示例** —— 仅当该文档列有完整字段表时追加三字段说明。

---

## Self-review（对照 spec）

| Spec 要求 | 对应 Task |
|---|---|
| `processing_error` 列 | Task 1 |
| Worker 失败 `failed` + error 截断 | Task 4 |
| API `processing_status` / `simple_status` / `error_message` | Task 2 |
| 重处理 cleanup 清 error | Task 2 |
| 成功 completed 清 error | Task 3 |
| 前端四态 Tag + 失败 Popover + 重处理 | Task 6–8 |
| 目录 null | Task 2 helper + Task 5 |
| 不做筛选/backfill | 未列入任务 ✓ |

**Placeholder scan:** 无 TBD；迁移 SQL 已给完整语句。

**Type consistency:** `simple_status` 字符串与前端 `SimpleKey` 一致；后端 `_SIMPLE_STATUS_MAP` 值与前端 union 一致。

---

## Execution handoff

**计划已保存至** `docs/superpowers/plans/2026-04-17-file-processing-status-display.md`。

**两种执行方式：**

1. **Subagent-Driven（推荐）** — 每个 Task 派生子代理执行，任务间人工/代理复核，迭代快。需配合 **subagent-driven-development** 技能。

2. **Inline Execution** — 本会话内按 Task 顺序实现，批量提交并在 Task 5、Task 8 后做检查点。需配合 **executing-plans** 技能。

你更倾向哪一种？
