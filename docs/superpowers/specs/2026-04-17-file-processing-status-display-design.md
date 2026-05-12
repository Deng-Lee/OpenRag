# File Processing Status Display Design

**Date:** 2026-04-17  
**Topic:** 文件处理状态展示 —— 补齐失败写入、API 输出、前端 4 类 Tag 及失败详情查看  
**Status:** Draft

## Overview

当前 `files.processing_status` 字段虽已存在且会在成功路径被更新（`parsing → building_hierarchy → embedding → completed`），但存在三个缺口：

1. **Worker 失败时从不写入 `failed`**，导致处理失败的文件会永久卡在中间状态（如 `parsing`）。
2. **API 不返回此字段**，`FileResponse` Pydantic 模型中完全没有它，前端拿不到。
3. **前端未展示**，用户无法感知文件的处理进度或失败。

本设计在**不改原有枚举**的前提下，补齐后端 → API → 前端的完整链路，并按用户视角把 6 个细粒度状态聚合为 4 个大类展示：未处理 / 处理中 / 已处理 / 处理失败。同时为失败文件提供"点击查看失败原因 + 一键重新处理"的交互闭环。

## Goals

1. Worker 处理 `process_document` 类任务异常时，把对应文件的 `processing_status` 标为 `failed`，并记录错误信息。
2. 文件相关 API 额外返回 `processing_status`（原始细粒度）与派生的 `simple_status`（4 类聚合）。
3. 文件列表新增"状态"列，用 antd `<Tag>` 呈现 4 类状态。
4. 处理失败的 Tag 支持点击查看错误详情，并提供"重新处理"快捷入口。

## Non-Goals

1. 不调整 `ProcessingStatus` 枚举值（保持 6 个细粒度状态）。
2. 不对历史数据做 backfill（即使文件已经有 `l0_path/l1_path/l2_path` 但状态仍为 `pending` 的脏数据，不自动修复）。
3. 不支持按状态筛选文件列表。
4. 不引入实时状态推送（WebSocket/SSE），前端依赖现有的刷新机制。
5. 不改造 worker 的重试机制（沿用当前"一次失败即最终失败"的行为）。
6. 不在 `delete_file` / `delete_path_prefix` 等非处理类任务失败时触达 `processing_status`。

## Architecture

整体流程（新增/修改部分粗体标注）：

```
Upload / Reprocess
      │
      ▼
   Worker._execute_task
      │  try: _process_document  ──► document_processor 正常路径写 parsing/.../completed
      │  except:
      │     **_mark_file_failed(file_id, error)**           ← 新增
      │       · files.processing_status = failed
      │       · files.processing_error  = error[:4096]      ← 新增列
      ▼
   tasks.status = failure (既有逻辑)
```

```
GET /files/ , GET /files/{id} , ...
      │
      ▼
   FileResponse (Pydantic)
      · processing_status  (str)   ← 新增
      · simple_status      (str)   ← 新增（派生）
      · error_message      (str?)  ← 新增（failed 时填充 files.processing_error）
      ▼
   FileList.tsx
      · 新增"状态"列，antd <Tag>
      · failed → <Popover> 展示 error_message + "重新处理"按钮
```

## Data Model

### `files` 表新增列

| 列名 | 类型 | Nullable | 说明 |
|---|---|---|---|
| `processing_error` | `TEXT` | Yes | 最近一次处理失败的错误信息；成功/重置时清空 |

- 使用 Alembic 迁移（新增 autogenerate 迁移，向上加列、向下删列）。
- SQLAlchemy 模型 `OpenRag/src/openrag/models/file.py` 相应增加 `Mapped[Optional[str]]`。
- 不做长度约束（PostgreSQL `TEXT`），但写入时应用层截断为 4096 字符，防止异常栈过长撑爆行。

### `ProcessingStatus` 枚举：保持不变

仍为 `pending / parsing / building_hierarchy / embedding / completed / failed`，**不新增、不合并、不修改**。

### 派生枚举 `SimpleStatus`

仅用于 API 响应，不入库：

| 原始 `processing_status` | `simple_status` |
|---|---|
| `pending` | `unprocessed` |
| `parsing` / `building_hierarchy` / `embedding` | `processing` |
| `completed` | `done` |
| `failed` | `failed` |

目录行（`is_directory = True`）：`processing_status` 与 `simple_status` 均返回 `null`，前端展示为 `-`。

## Backend Changes

### 1. Worker 失败标记

文件：`OpenRag/src/openrag/worker/task_worker.py`

- 新增私有方法：

  ```python
  def _mark_file_failed(self, file_id: int, error: str) -> None:
      """标记文件为处理失败，并写入错误信息。
      独立 session，容错（本身异常不应影响 task 状态更新）。"""
  ```

- 修改 `_execute_task` 的 `except Exception as e` 分支：

  ```python
  except Exception as e:
      task_type = task.get("task_type") or "process_document"
      file_id = task.get("file_id")
      if task_type == "process_document" and file_id:
          try:
              self._mark_file_failed(file_id, str(e))
          except Exception as ex:
              _logger.warning("Failed to mark file %s as failed: %s", file_id, ex)
      self._update_task_status(task_id, "failure", error=str(e))
  ```

- `_mark_file_failed` 实现要点：
  - 开独立 `SessionLocal()`，try/finally close
  - 将 `error` 截断到 4096 字符
  - 同时设置 `processing_status = failed` 与 `processing_error = error[:4096]`

### 2. 重处理入口清理错误信息

文件：`OpenRag/src/openrag/api/files_api.py::cleanup_file_processing_data`

在已有的重置逻辑后追加：

```python
file.processing_error = None
```

（确保重处理成功后上一次的错误信息不残留。）

### 3. `FileResponse` schema 扩展

文件：`OpenRag/src/openrag/api/files_api.py`

```python
SIMPLE_STATUS_MAP: dict[str, str] = {
    "pending": "unprocessed",
    "parsing": "processing",
    "building_hierarchy": "processing",
    "embedding": "processing",
    "completed": "done",
    "failed": "failed",
}

class FileResponse(BaseModel):
    # ... 既有字段
    processing_status: Optional[str] = None
    simple_status: Optional[str] = None
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}
```

- 所有返回 `FileResponse` / `FileUploadResponse` / `FileListResponse` 的接口新增一个内部 helper：

  ```python
  def _to_file_response(file: FileModel) -> FileResponse:
      ps = None if file.is_directory else (
          file.processing_status.value if file.processing_status else None
      )
      ss = SIMPLE_STATUS_MAP.get(ps) if ps else None
      err = file.processing_error if ss == "failed" else None
      return FileResponse(
          id=file.id, uri=file.uri, name=file.name, owner_id=file.owner_id,
          parent_id=file.parent_id, is_directory=file.is_directory,
          size=file.size, mime_type=file.mime_type,
          created_at=file.created_at.isoformat(),
          updated_at=file.updated_at.isoformat(),
          processing_status=ps, simple_status=ss, error_message=err,
      )
  ```

- 所有涉及的端点（upload / list / get / reprocess / directories / move）统一改用 `_to_file_response`。

### 4. 测试

- 单测 `test_simple_status_map`：6 个原值 → 4 个聚合值的映射
- 单测 `_mark_file_failed`：设置 status + error，截断长错误
- 单测 `cleanup_file_processing_data`：重置后 `processing_error` 为 None
- 集成测试：上传失败场景 → `GET /files/{id}` 返回 `simple_status=failed` 且 `error_message` 非空

## Frontend Changes

### 1. 类型扩展

文件：`web/src/types.ts`（或 `File` 类型定义所在处）

```typescript
export type SimpleStatus = 'unprocessed' | 'processing' | 'done' | 'failed';

export interface File {
  // ... 既有字段
  processing_status?: string | null;
  simple_status?: SimpleStatus | null;
  error_message?: string | null;
}
```

### 2. `FileList.tsx` 新增"状态"列

位置：插入在"上传时间"列之前（保持操作列固定在最右侧）。

```typescript
const STATUS_CONFIG: Record<SimpleStatus, { color: string; labelKey: string }> = {
  unprocessed: { color: 'default',    labelKey: 'files.status.unprocessed' },
  processing:  { color: 'processing', labelKey: 'files.status.processing'  },
  done:        { color: 'success',    labelKey: 'files.status.done'        },
  failed:      { color: 'error',      labelKey: 'files.status.failed'      },
};
```

渲染规则：

- 目录行（`is_directory = true`）：显示 `-`
- 非目录行 + `simple_status` 为空：显示 `-`（兼容老接口）
- `simple_status != 'failed'`：`<Tag color={cfg.color}>{t(cfg.labelKey)}</Tag>`
- `simple_status == 'failed'`：用 `<Popover>` 包裹 Tag，trigger=`click`：
  - 标题：`t('files.status.failed_title')`
  - 内容：`<pre>` 展示 `error_message`（`white-space: pre-wrap`、`max-height: 240px`、纵向滚动）
  - 底部：`<Button type="link" icon={<ReloadOutlined />} onClick={() => handleReprocess(record)}>` 复用现有 `handleReprocess` 打开重处理弹窗

### 3. i18n

`web/src/i18n/locales/zh.json` 与 `en.json` 新增：

```json
{
  "files": {
    "columns": { "status": "状态" | "Status" },
    "status": {
      "unprocessed":  "未处理" | "Unprocessed",
      "processing":   "处理中" | "Processing",
      "done":         "已处理" | "Done",
      "failed":       "处理失败" | "Failed",
      "failed_title": "处理失败"  | "Processing failed",
      "no_error":     "没有错误详情" | "No error detail"
    }
  }
}
```

### 4. 测试

`web/src/components/FileList.test.tsx` 新增场景：

- 四种 `simple_status` 各渲染一行，断言 Tag 文案与颜色
- `failed` 行：点击 Tag 后 Popover 可见，展示 `error_message`
- `failed` 行：点击 Popover 内的"重新处理"按钮，打开重处理弹窗（复用已有断言模式）
- 目录行：状态列显示 `-`
- `error_message` 为 null 的 `failed` 行：Popover 内显示 `files.status.no_error` 占位

## Data Flow

```
Upload or Reprocess
    │
    ▼
 Broker/Task Queue
    │
    ▼
 TaskWorker._execute_task
    ├── success ─► document_processor 写 completed
    └── exception ─► _mark_file_failed(file_id, error)
                     · files.processing_status = failed
                     · files.processing_error  = error
    ▼
 tasks.status = failure （既有）

---

 FileList.tsx
    │
    ▼
 GET /files/?workspace_id=...
    │
    ▼
 files_api.list_files  ─► _to_file_response(file)
    │
    ▼
 {processing_status, simple_status, error_message}
    │
    ▼
 <Tag> or <Popover><Tag/></Popover>
```

## Error Handling

| 场景 | 处理 |
|---|---|
| Worker 抛异常，但 `_mark_file_failed` 自身也失败 | 记录 warning 日志，不阻塞 task 状态更新 |
| `error` 字符串超长 | 应用层截断到 4096 字符写入 `processing_error` |
| `processing_status` 值无法映射到 `simple_status` | 返回 `null`（前端展示 `-`） |
| 目录被误写入 `processing_status` | API 仍返回 `null`（`_to_file_response` 里判 `is_directory`） |
| 前端 `error_message` 为空/null 但状态是 failed | Popover 展示占位文案 `no_error` |

## Security Considerations

- `error_message` 可能包含本地路径或堆栈，但不含用户敏感数据；只向对该文件有 read 权限的用户返回（由既有权限逻辑兜底）。
- 截断到 4096 字符兼顾可读性与存储成本；避免超大 error 撑爆行。

## Migration / Rollout

1. 生成 Alembic 迁移：`alembic revision --autogenerate -m "add files.processing_error"`
2. `alembic upgrade head`
3. 部署后端（包含 worker 新失败写入 + API 新字段）
4. 部署前端
5. 验证：故意上传一个会失败的文件（如空文件、损坏的 PDF），确认"处理失败"Tag 出现且 Popover 内能看到错误信息

## Testing Considerations

1. 后端单测：状态映射、`_mark_file_failed`、cleanup 清理 error
2. 后端集成测试：模拟处理失败场景下 `GET /files/{id}` 的字段完整性
3. 前端组件测试：四种状态 + 失败 Popover + 重处理入口
4. 手工验收：
   - 上传一个成功文件 → 列表状态从 `处理中` 变 `已处理`
   - 上传一个故意失败文件 → 列表状态变 `处理失败`，点击查看 error，点"重新处理"打开弹窗
   - 重处理成功后：error 详情消失，Tag 回到绿色

## Open Questions

None at this time.
