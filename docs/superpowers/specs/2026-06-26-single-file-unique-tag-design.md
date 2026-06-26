# 单文档唯一 tag + 按 tag 检索 — 设计文档

- 日期：2026-06-26
- 状态：设计待评审（Design / pending review）
- 范围：后端数据模型 1 列 + 1 迁移；写入 chokepoint `ingest_new_file()` 加 1 个可选参数；2 个上传入口各透传；新增 1 个外部只读检索端点；前端单文件上传弹窗加 tag 输入 + 批量守卫

## 1. 背景与目标

在**单个文档上传**时可为该文档指定一个**唯一 tag**，之后外部系统通过 service token 调用 API、用这个 tag 直接取到指定文档。

- 加 tag **仅限单文件上传**场景，覆盖两类来源：OpenRag 内部 UI 触发的上传，以及外部 API（service token）触发的上传。
- 上传时需校验 tag 是否与已有 tag 重合，重合则拒绝。
- 新增外部 API：按 tag 查询并返回该文档（元数据）。
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
| tag 默认约束（评审可改） | 长度上限 **128**；**区分大小写精确匹配**；纯空白视为不打 tag（NULL）；删除文件即释放 tag |

## 3. 关键现状（代码事实）

- **所有"新文件上传"都汇聚到单一函数** `ingest_new_file()`（[file_ingest.py:345](../../../openrag/src/openrag/services/file_ingest.py)）；它做校验、写 MinIO、建 `File` 行、按 MIME 建 `process_document` 任务。这是唯一需要插入 tag 的后端 chokepoint。
- 新上传入口仅两个，均单文件、均调用 `ingest_new_file()`：
  - `POST /files/upload`（[files_api.py:348](../../../openrag/src/openrag/api/files_api.py)）— 内部 UI / JWT 用户。
  - `POST /service/v1/workspaces/{workspace_name}/documents`（[service_api.py:295](../../../openrag/src/openrag/api/service_api.py)）— 外部 service token。
- `PUT /service/v1/workspaces/{workspace_name}/documents/by-path`（[service_api.py:329](../../../openrag/src/openrag/api/service_api.py)）是**替换已有文件内容**（走 `replace_file_content`，沿用旧 `File` 行），不是新建 → 不涉及 tag。
- 数据源连接器（GitHub / Google Drive / Jira / Bitbucket 等，`openrag/common/data_source/`）是**批量同步**，不走上传端点 → 不涉及。
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

## 5. 边界与不做（Non-goals）

- **不**支持给已存在文件改 / 加 / 删 tag（tag 只在新建上传时设定）；替换内容、reprocess 均不动 tag。
- 删除文件即释放其 tag（行删除，unique 索引自然释放）。
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

**前端**（`FileUpload` 相关测试）：

- 单文件 + 填写 tag → `filesAPI.upload` 收到 tag 参数。
- 填写 tag 后拖文件夹/多文件 → 被拦截、不调用上传、出现 warning。
- 未填 tag 拖文件夹 → 批量正常、不带 tag（现状不回归）。

## 7. 影响文件清单

- `openrag/src/openrag/models/file.py`（`tag` 列 + `@validates` + `UniqueConstraint`）
- `openrag/alembic/versions/20260626_0004_add_file_tag.py`（新建迁移）
- `openrag/src/openrag/services/file_ingest.py`（`ingest_new_file` 加 `tag` 参数 + 校验 + 竞态兜底）
- `openrag/src/openrag/api/files_api.py`（`/upload` 加 `tag` Form；`FileUploadResponse` / `_file_to_upload_response` 回显 tag）
- `openrag/src/openrag/api/service_api.py`（`/documents` 加 `tag` Form；新增 `GET /documents/by-tag`；`_upload_response_dict` / `_document_summary` 带 tag）
- 文件详情/列表响应 schema（增加可选 `tag` 字段）
- `web/src/components/FileUpload.tsx`（tag 输入 + 批量守卫 + 透传 + 清空 + 409 提示）
- `web/src/services/api.ts`（`upload()` 加可选 `tag`）
- `web/src/i18n/locales/zh.json`、`web/src/i18n/locales/en.json`（tag 相关文案）
- 相应后端与前端测试文件
