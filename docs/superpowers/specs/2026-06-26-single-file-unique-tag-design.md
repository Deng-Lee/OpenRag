# 单文档唯一 tag + 按 tag 检索/替换/删除 — 设计文档

- 日期：2026-06-26
- 状态：设计待评审（Design / pending review）
- 范围：后端数据模型 1 列 + 1 迁移；写入 chokepoint `ingest_new_file()` 加 1 个可选参数；2 个上传入口各透传；新增 3 个外部 service-token 端点（按 tag 检索 / 按 tag 替换内容 / 按路径删除）；删除触发时同步释放 tag；前端单文件上传弹窗加 tag 输入 + 批量守卫

## 1. 背景与目标

在**单个文档上传**时可为该文档指定一个**唯一 tag**，之后外部系统通过 service token 调用 API、用这个 tag 直接取到指定文档。

- 加 tag **仅限单文件上传**场景，覆盖两类来源：OpenRag 内部 UI 触发的上传，以及外部 API（service token）触发的上传。
- 上传时需校验 tag 是否与已有 tag 重合，重合则拒绝。
- 新增外部 API：按 tag 查询并返回该文档（元数据）。
- 补齐外部按 tag 的文档生命周期：按 tag **刷新内容**（`PUT by-tag`，保留 tag）、按路径**删除**文档（`DELETE by-path`，释放 tag）；删除触发时**同步释放 tag**、重清理异步。
- 对现有逻辑做**外科手术式**改动，不重构无关代码。

## 2. 已确认的决策

| 决策点 | 选择 |
|---|---|
| tag 唯一性范围 | **全局唯一**（整个系统内唯一，tag → 文档 1:1）。检索 `GET /service/v1/documents/by-tag`，不挂在 `/workspaces/{name}` 下 |
| 检索返回内容 | **仅元数据**（同现有 `documents/by-path` 形态，附 `tag` + `workspace_id`/`workspace_name`） |
| 检索鉴权与跨区探测 | service token 鉴权；查到文档后校验 token 对其 workspace 有 read 权限，**无权一律 404**（非 403），防止跨工作区探测 tag 是否存在 |
| 存储方案 | **在 `files` 表加一列 `tag`**（非独立 `file_tags` 表）；unique 索引同时服务唯一性校验与检索查询 |
| 前端"只在单文件加 tag" | tag 非空且拖入文件夹/多文件时，**整批拦截并提示**；tag 为空时文件夹/批量照常、不加 tag |
| 替换 / reprocess / 数据源连接器 | **不改**——tag 只在新建文件时设定 |
| 外部删除（新增） | 新增 `DELETE /service/v1/workspaces/{name}/documents/by-path`，复用内部删除链路，默认异步 202、可选同步 200 |
| 删除时同步释放 tag | 触发删除即把该行 `tag` 同步置 NULL（重清理仍异步），tag 当场可复用；内部 `DELETE /files/{id}` 一并处理（**已确认：内外都加**） |
| 上传撞 tag 的处理 | 一律 **409 拒绝**，**不做** upload-time 自动 reassign/upsert；"挪 tag" 走显式两步（DELETE 旧 → POST 新）或 `PUT by-tag` 同路径替换 |
| 按 tag 替换内容（新增） | 新增 `PUT /service/v1/documents/by-tag`：按 tag 定位后复用 `replace_file_content` 原地换内容、保留 tag/uri、重新处理、**无竞态**；tag 不存在 → 404 |
| tag 默认约束（评审可改） | 长度上限 **128**；**区分大小写精确匹配**；纯空白视为不打 tag（NULL）；删除文件即释放 tag |

## 3. 关键现状（代码事实）

- **所有"新文件上传"都汇聚到单一函数** `ingest_new_file()`（[file_ingest.py:345](../../../openrag/src/openrag/services/file_ingest.py)）；它做校验、写 MinIO、建 `File` 行、按 MIME 建 `process_document` 任务。这是唯一需要插入 tag 的后端 chokepoint。
- 新上传入口仅两个，均单文件、均调用 `ingest_new_file()`：
  - `POST /files/upload`（[files_api.py:348](../../../openrag/src/openrag/api/files_api.py)）— 内部 UI / JWT 用户。
  - `POST /service/v1/workspaces/{workspace_name}/documents`（[service_api.py:295](../../../openrag/src/openrag/api/service_api.py)）— 外部 service token。
- `PUT /service/v1/workspaces/{workspace_name}/documents/by-path`（[service_api.py:329](../../../openrag/src/openrag/api/service_api.py)）是**替换已有文件内容**（走 `replace_file_content`，沿用旧 `File` 行），不是新建 → 不涉及 tag。
- `replace_file_content`（[file_ingest.py:589](../../../openrag/src/openrag/services/file_ingest.py)）在**传入的同一 `File` 行**上操作：清旧 chunks/向量、覆盖**同一 `uri`** 的 MinIO 对象、重入队 `process_document`；**不建新行、不改 `uri`、不触碰 `tag`**（第 632-645 行只改 parser_type/document_type/size/mime_type）→ 天然适合"按 tag 原地刷新内容"，无 409/竞态。
- 数据源连接器（GitHub / Google Drive / Jira / Bitbucket 等，`openrag/common/data_source/`）是**批量同步**，不走上传端点 → 不涉及。
- **删除现状**：外部 service-token API（`service_api.py`）**没有任何 `@router.delete`**——外部只能上传(POST)与替换内容(PUT by-path)，删不了文档。唯一的文档删除是内部 `DELETE /files/{file_id}`（[files_api.py:741](../../../openrag/src/openrag/api/files_api.py)，JWT `get_current_user` + owner/admin write）：默认异步建 `TaskType.DELETE_FILE` 任务，`background=false` 时同步调 `delete_file_with_storage(db, file, workspace)`。删除的统一 chokepoint 为 `delete_file_with_storage`（[file_deletion.py:82](../../../openrag/src/openrag/services/file_deletion.py)）；`TaskType.DELETE_FILE` 见 [task.py:36](../../../openrag/src/openrag/models/task.py)。
- `File` 模型（[file.py](../../../openrag/src/openrag/models/file.py)）**无 tag 字段**；已有 `UniqueConstraint("workspace_id", "uri", name="uq_files_workspace_uri")`（[file.py:175](../../../openrag/src/openrag/models/file.py)）；`@validates(...)` 在 [file.py:84](../../../openrag/src/openrag/models/file.py) 对若干字符串列做 NUL 清洗。
- `service_api.py` 已有可复用 helper：`_token_can_read_workspace(ctx, workspace_id)`（[service_api.py:228](../../../openrag/src/openrag/api/service_api.py)）、`_document_summary(f)`（[service_api.py:172](../../../openrag/src/openrag/api/service_api.py)）、`_upload_response_dict(...)`（[service_api.py:184](../../../openrag/src/openrag/api/service_api.py)）、`require_workspace_for_name` / `assert_token_workspace_permission`。
- 前端上传弹窗组件 `FileUpload.tsx`：
  - 单个文件（点击选择 / 拖单文件）走 antd `Dragger` 的 `customRequest`（[FileUpload.tsx:305](../../../web/src/components/FileUpload.tsx)）。
  - 文件夹拖拽由 capture 阶段的 `onDrop`（[FileUpload.tsx:273](../../../web/src/components/FileUpload.tsx)）用 `webkitGetAsEntry().isDirectory` 检测后接管，走批量循环 `runFolderUpload()`（[FileUpload.tsx:199](../../../web/src/components/FileUpload.tsx)）。
  - `Dragger` 设 `multiple: false`（[FileUpload.tsx:303](../../../web/src/components/FileUpload.tsx)）：点击选择只能选 1 个；拖多个散文件时 rc-upload 一般只取第 1 个。
  - 两条路径都调用 `filesAPI.upload(file, parserType, workspaceId, path, documentType)`（[api.ts:103](../../../web/src/services/api.ts)）→ `POST /files/upload`。
- 数据库：生产 PostgreSQL、测试 SQLite；两者的 **UNIQUE 索引都允许多个 NULL** → 未打 tag 的文件互不冲突。
- Alembic 迁移规范：版本号 `YYYYMMDD_NNNN`，当前 head 为 `20260602_0003`（[20260602_0003_add_file_document_type.py](../../../openrag/alembic/versions/20260602_0003_add_file_document_type.py)），范式为 `op.add_column` / `op.create_index`。

## 4. 方案设计

### 4.1 数据模型 + 迁移

- `File` 模型（[file.py](../../../openrag/src/openrag/models/file.py)）：
  - 新增列 `tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="External unique tag (single-file upload only)")`。
  - 把 `"tag"` 加入第 84 行 `@validates(...)` 列表，复用现有 NUL 清洗。
  - `__table_args__` 追加 `UniqueConstraint("tag", name="uq_files_tag")`（全局唯一；多 NULL 允许）。
- 新迁移 `openrag/alembic/versions/20260626_0004_add_file_tag.py`（`down_revision="20260602_0003"`）：
  - `op.add_column("files", sa.Column("tag", sa.String(length=128), nullable=True))`
  - `op.create_unique_constraint("uq_files_tag", "files", ["tag"])` — 与模型里的 `UniqueConstraint` 种类一致，避免 autogenerate 漂移；PG 以 unique 索引实现，等值查询 `WHERE tag = ?` 同样走索引。
  - `downgrade`：先 `op.drop_constraint("uq_files_tag", "files", type_="unique")` 再 `op.drop_column("files", "tag")`。
  - 注：测试用 SQLite 经模型 `Base.metadata.create_all()` 建表（不跑迁移），约束直接来自模型声明；迁移只需在 PG 跑通。

### 4.2 写入链路（唯一 chokepoint）

`ingest_new_file()`（[file_ingest.py:345](../../../openrag/src/openrag/services/file_ingest.py)）新增**关键字参数** `tag: Optional[str] = None`：

- 规范化：`tag = (tag or "").strip() or None`；若 `tag` 非空且长度 > 128 → `HTTPException(400)`。
- **重复校验**（紧挨现有"重复 URI 预检查" [file_ingest.py:461-483](../../../openrag/src/openrag/services/file_ingest.py)，即在写 MinIO 之前）：若 `tag` 非空且 `db.query(File).filter(File.tag == tag).first()` 命中 → `HTTPException(409, "Tag already in use")`。早于 MinIO 写入，错误干净。
- 创建 `File(..., tag=tag)`。
- **竞态兜底**：`db.commit()` 若因 `uq_files_tag` 抛 `IntegrityError` → 回滚并转 `HTTPException(409)`（覆盖两个并发上传抢同一 tag 的极小概率；此分支下 MinIO 已写入会留下孤儿对象，与现有任何"commit 后失败"的行为一致，不在本次额外处理）。
- 其余流程（trace span、任务创建、返回 `(file_record, task_record)`）完全不变。

### 4.3 两个上传入口（各加一个可选参数透传）

- `POST /files/upload`（[files_api.py:348](../../../openrag/src/openrag/api/files_api.py)）：新增 `tag: Optional[str] = Form(default=None, description="Unique tag (single-file upload only)")`，在调用处透传 `ingest_new_file(..., tag=tag)`。
- `POST /service/v1/workspaces/{workspace_name}/documents`（[service_api.py:295](../../../openrag/src/openrag/api/service_api.py)）：同样新增 `tag` Form 参数并透传。
- **不改**：`PUT .../by-path`（替换）、`reprocess`、数据源连接器。

### 4.4 检索入口（新增，仅外部 service token）

新增 `GET /service/v1/documents/by-tag`（放在 `service_api.py`，因全局唯一**不挂在** `/workspaces/{name}` 下）：

```
GET /service/v1/documents/by-tag?tag=XXX
```

- 鉴权依赖 `get_service_token_context`（与其他 service 路由一致）。
- 逻辑：
  1. `f = db.query(File).filter(File.tag == tag, File.is_directory.is_(False)).first()`；为空 → `404`。
  2. 权限：`_token_can_read_workspace(ctx, f.workspace_id)`（[service_api.py:228](../../../openrag/src/openrag/api/service_api.py)）为假 → **`404`**（不是 403，避免跨工作区探测）。
  3. 返回元数据 dict：复用 `_document_summary(f)` 的字段，额外补 `tag`、`workspace_id`、`workspace_name`（全局检索方需要知道文档落在哪个工作区）。

### 4.5 tag 可见性（让 UI / 调用方看到 tag）

- 上传响应回显 tag：
  - service：`_upload_response_dict`（[service_api.py:184](../../../openrag/src/openrag/api/service_api.py)）增加 `"tag": file_record.tag`。
  - 内部：`FileUploadResponse` 及 `_file_to_upload_response`（files_api.py）增加可选 `tag` 字段。
- 文件详情 / 列表响应 schema（FileResponse 等）增加可选 `tag` 字段，便于内部 UI 展示（纯增量、低风险；`_document_summary` 同步带上 `tag`）。

### 4.6 前端（只在单文件路径加 tag）

`FileUpload.tsx`：

- 在弹窗表单内新增可选「唯一标签（tag）」`Input`，状态 `const [tag, setTag] = useState("")`，并用 `useRef` 镜像（与 `uploadPathRef` 等一致，供事件回调读取最新值）。
- **单文件路径**：`customRequest`（[FileUpload.tsx:305](../../../web/src/components/FileUpload.tsx)）调用 `filesAPI.upload(...)` 时把 `tag.trim() || undefined` 作为新参数传入。
- **批量守卫**（实现"拦截并提示"）：扩展 capture 阶段 `onDrop`（[FileUpload.tsx:273](../../../web/src/components/FileUpload.tsx)）——当 `tagRef.current` 非空且（检测到目录 `hasDirectory` 或散文件数 > 1）时：`preventDefault()` + `stopPropagation()` + `message.warning("已填写唯一标签，仅支持单文件上传；如需上传文件夹/批量，请先清空标签")` 并 `return`，**不发生任何上传**。
- **批量路径** `runFolderUpload()`（[FileUpload.tsx:199](../../../web/src/components/FileUpload.tsx)）：保持不读取 tag（守卫已保证 tag 非空时不会进入此路径）。
- 上传成功后清空 tag（与现有 `setParserType('auto')` 等一并 reset）。
- `api.ts`：`filesAPI.upload()`（[api.ts:103](../../../web/src/services/api.ts)）新增可选 `tag?: string` 形参，非空时 `formData.append('tag', tag)`。
- 409 冲突：`customRequest` 的 catch 分支识别 409 → 友好提示「该标签已被占用，请换一个」。
- i18n：新增 `files.upload.tag_label` / `files.upload.tag_conflict` / `files.upload.tag_batch_blocked`（`zh.json`/`en.json` 各一条）。
- 文件列表/详情若存在 tag 则展示（沿用现有渲染，纯增量）。

### 4.7 外部删除入口（新增，仅外部 service token）

新增 `DELETE /service/v1/workspaces/{workspace_name}/documents/by-path`，与现有 `PUT .../by-path` 同一 URL 形态、对称：

```
DELETE /service/v1/workspaces/{workspace_name}/documents/by-path?path=XXX&background=true
```

- 鉴权：`require_workspace_for_name(db, workspace_name)` + `assert_token_workspace_permission(ctx, ws.id, "write")`（删除属写操作）。
- 定位文件：`db.query(File).filter(File.workspace_id == ws.id, File.uri == validate_path(path), File.is_directory.is_(False)).first()`；为空 → `404`（沿用 PUT by-path 的查找方式 [service_api.py:341-347](../../../openrag/src/openrag/api/service_api.py)）。
- 删除：**复用内部同款删除链路，不重写**：
  - `background=true`（默认，`Query(default=True)`）：**先同步把 `row.tag = None` 并 commit（即时释放 tag）**，再 `TaskService.create_task(workspace_id=ws.id, user_id=ws.owner_id, file_id=row.id, task_type=TaskType.DELETE_FILE.value, queue="normal", priority=6, max_retries=3, status=TaskStatus.PENDING)` → 返回 `202 + task_id`。重清理（向量/对象/删行）仍异步。
  - `background=false`：`delete_file_with_storage(db, row, ws)`（[file_deletion.py:82](../../../openrag/src/openrag/services/file_deletion.py)）同步清 MinIO/层级/Milvus/DB（含删行，tag 随行消失）→ 返回 `200`。
- `user_id` 取 `ws.owner_id`，与 service 上传的操作者归属一致。
- **同步释放 tag 的安全性**：清理任务全程按 `file.id`（向量/ES/Task/删行）与 `file.uri`（MinIO 对象）作用，**无一处按 tag**（[file_deletion.py:109-117](../../../openrag/src/openrag/services/file_deletion.py)）；且 `(workspace_id, uri)` 唯一约束禁止同 uri 两行并存。故"删后用同一 tag 新建另一文档（不同路径）"绝不会被旧行的异步清理波及；同路径复用仍受 uri 约束、需等清理完（或改用 `PUT by-tag` 替换 / `background=false`）。
- 为与外部一致，内部 `DELETE /files/{id}` 入队 `DELETE_FILE` 前同样同步置 `tag=None`（**已确认：内外都加**）。
- 仅删**单个文件**；目录/前缀级联删除不在本次范围（内部已有 `POST /files/delete-path-prefix`）。
- 需在 `service_api.py` 增加 import：`delete_file_with_storage`、`TaskService`、`TaskType`、`TaskStatus`。

### 4.8 按 tag 替换内容入口（新增，仅外部 service token）

新增 `PUT /service/v1/documents/by-tag`（全局，**不挂在** `/workspaces/{name}` 下，与 GET by-tag 对称），用于「按 tag 原地刷新文档内容、路径不变」：

```
PUT /service/v1/documents/by-tag?tag=XXX&parser_type=auto   （body: 新文件 multipart）
```

- 逻辑：
  1. `row = db.query(File).filter(File.tag == tag, File.is_directory.is_(False)).first()`；为空 → `404`（tag 未被使用；调用方据此改走 `POST upload` 创建）。
  2. 权限（写操作，分层判定以兼顾防探测）：`_token_can_read_workspace(ctx, row.workspace_id)` 为假 → `404`（连看都看不到）；可读但不可写 → `403`。需新增 `_token_can_write_workspace(ctx, workspace_id)`（与现有 `_token_can_read_workspace` 对称）。
  3. `ws = 该行所属 workspace`；调用 `replace_file_content(db, ws, ws.owner_id, row, new_content=body, content_type=file.content_type, parser_type=parser_type)`（复用现成函数，保留 `row.tag`/`row.uri`、清旧块/向量、重入队处理）。
  4. 返回更新后的元数据 dict（含 `tag`）。
- 无删除、无新建 → **不撞 `(workspace_id, uri)`、不撞 `tag`、无 409、无竞态**；`created_at`/`id`/`tag` 均保留。
- 仅替换**已存在**文档的内容；若要换路径/文件名，属「删 + 新建」（不同路径，已天然无竞态）。

## 5. 边界与不做（Non-goals）

- **不**支持**修改 tag 的值**（改名 / 重打 / 清空）——tag 一经设定即不可变，reprocess 不动 tag。能做的是：`PUT by-tag` **按 tag 刷新内容**（保留同一 tag）、`DELETE` **释放 tag**（删文档）、删后用同一 tag 新建另一文档。
- 上传撞**已被占用的 tag** 一律 **409**；**不做** upload-time 自动 reassign / upsert。"把 tag 挪到另一文档"请显式两步：`DELETE` 旧（异步，tag 同步释放）→ `POST` 上传新；同路径刷新用 `PUT by-tag`。
- 文件唯一性键是**完整 `uri`（含目录）**，**裸文件名不是唯一性键**：同名不同目录（`/a/doc.pdf` vs `/b/doc.pdf`）本就并存。故"删旧（异步）→ 在同 workspace **不同路径、即使同文件名**建同 tag 新文档"**安全**——异步清理严格按旧行的 `id`/`uri` 作用，碰不到新行（新行 id、uri 均不同）。
- 删除文件即释放其 tag（行删除，unique 约束自然释放）。外部经新增的 `DELETE .../documents/by-path` 即可走完此路径；**目录/前缀级联删除**仍不在本次范围（内部 `POST /files/delete-path-prefix` 承担）。
- 全局唯一的固有性质：上传时 tag 若与**他人工作区**文档撞车也会 409（提示语保持通用「标签已被占用」，不透露在哪个工作区）。
- **不**为数据源连接器（批量同步）加 tag。
- **不**新增内部 JWT 的按 tag 检索端点（检索仅外部 service token；内部 UI 通过文件元数据里的 tag 字段查看）。
- tag 大小写敏感、精确匹配；**不**做模糊/前缀检索。

## 6. 测试

**后端**（沿用现有 PostgreSQL / SQLite 测试风格）：

- 带 tag 单文件上传 → `File.tag` 落库；响应回显 tag。
- 重复 tag 上传 → 409（预检查路径）。
- 多个**无 tag** 文件 → 互不冲突（多 NULL）。
- `GET /service/v1/documents/by-tag` 命中 → 返回元数据 + tag + workspace 信息。
- 无权 token 查他人工作区的 tag → 404（非 403）。
- 未知 tag → 404。
- 超长 tag → 400。
- （可选）并发抢同一 tag → 恰一个成功、另一个 409。
- `DELETE /service/v1/workspaces/{name}/documents/by-path` 删除文件 → 文档消失、其 tag 释放、可用同一 tag 重新上传成功。
- 无 write 权限 token 删除 → 403；删不存在的 path → 404。
- 异步删除（`background=true`）返回后，该 tag **立即**可用于新文档（**不同路径、即使同文件名**）且不 409；旧行异步清理只按 id/uri 作用，不影响新行。
- `PUT /service/v1/documents/by-tag` 替换内容 → 同一行 id/uri/**tag 不变**、内容更新、重新处理、无 409；tag 不存在 → 404；有 read 无 write → 403。

**前端**（`FileUpload` 相关测试）：

- 单文件 + 填写 tag → `filesAPI.upload` 收到 tag 参数。
- 填写 tag 后拖文件夹/多文件 → 被拦截、不调用上传、出现 warning。
- 未填 tag 拖文件夹 → 批量正常、不带 tag（现状不回归）。

## 7. 影响文件清单

- `openrag/src/openrag/models/file.py`（`tag` 列 + `@validates` + `UniqueConstraint`）
- `openrag/alembic/versions/20260626_0004_add_file_tag.py`（新建迁移）
- `openrag/src/openrag/services/file_ingest.py`（`ingest_new_file` 加 `tag` 参数 + 校验 + 竞态兜底）
- `openrag/src/openrag/api/files_api.py`（`/upload` 加 `tag` Form；`FileUploadResponse` / `_file_to_upload_response` 回显 tag；`DELETE /files/{id}` 异步分支入队前同步置 `tag=None`（已确认内外一致））
- `openrag/src/openrag/api/service_api.py`（`/documents` 加 `tag` Form；新增 `GET /documents/by-tag`、`PUT /documents/by-tag`（复用 `replace_file_content`）、`DELETE /workspaces/{name}/documents/by-path`；新增 `_token_can_write_workspace` helper；新增 import `delete_file_with_storage` / `TaskService` / `TaskType` / `TaskStatus`；`_upload_response_dict` / `_document_summary` 带 tag）
- 文件详情/列表响应 schema（增加可选 `tag` 字段）
- `web/src/components/FileUpload.tsx`（tag 输入 + 批量守卫 + 透传 + 清空 + 409 提示）
- `web/src/services/api.ts`（`upload()` 加可选 `tag`）
- `web/src/i18n/locales/zh.json`、`web/src/i18n/locales/en.json`（tag 相关文案）
- 相应后端与前端测试文件
