# 单文档唯一 tag（workspace 内唯一）+ 按 tag 检索 / upsert / 删除 — 设计文档

- 日期：2026-06-26（Codex 评审处置 + 收敛：2026-06-28）
- 状态：设计待评审（Design / pending review）
- 范围：后端数据模型 **2 列**（`tag`、`deleted_at`）+ 1 迁移；写入 chokepoint `ingest_new_file()` 加 `tag` 参数（字符集校验 + workspace 内查重 + 查重前置于建目录 + 按约束名区分 `IntegrityError`）；2 个上传入口透传；新增 3 个**workspace-scoped** 外部 service-token 端点（按 tag 检索 / upsert：建·改·**同行移动** / 按路径删除）；删除统一走「软删除 `deleted_at` + 释放 tag + 入队物理清理」**同事务**；所有读/写/预览/检索路径按 `deleted_at IS NULL` 过滤 active 文件；前端单文件 tag 输入 + 批量守卫。

> 本版已采纳 Codex 评审（见 §8）全部 10 条，并据用户 2026-06-28 决策收敛：tag 改为 **`(workspace_id, tag)` 内唯一**、upsert 异路径采用 **同一行 move+replace**、引入 **`deleted_at` 软删除**。

## 1. 背景与目标

在**单个文档上传**时为该文档指定一个 **workspace 内唯一的 tag**，外部系统通过 service token 用「workspace + tag」直接取到、刷新或删除该文档。

- 加 tag **仅限单文件上传**：OpenRag 内部 UI 上传 + 外部 API（service token）上传。
- 上传时校验 tag 在**当前 workspace 内**是否重复，重复则拒绝。
- 新增外部 API：workspace-scoped 的按 tag 查询（元数据）。
- 外部按 tag 的文档生命周期：统一 `PUT upsert-by-tag`（建 / 改 / **同行移动**）、`DELETE by-path`（纯删）。
- 删除统一**软删除**：入队即 `deleted_at=now()` + 释放 tag，对读立即不可见，物理清理（向量/对象/行）异步。
- 对现有逻辑做**外科手术式**改动，不重构无关代码（`deleted_at` 读过滤是唯一较广的横切点，须逐条审计读路径）。

## 2. 已确认的决策

| 决策点 | 选择 |
|---|---|
| tag 唯一性范围 | **`(workspace_id, tag)` 内唯一**（不同 workspace 可复用同一 tag；无跨区 409 侧信道）。按 tag 读写均 **workspace-scoped** |
| 检索入口与鉴权 | `GET /service/v1/workspaces/{name}/documents/by-tag`；`require_workspace_for_name` + `assert_token_workspace_permission(read)`（无 read → **403**，与现有 by-path 一致；workspace 显式给出，无跨区探测问题） |
| 检索返回内容 | **仅元数据**（同现有 `documents/by-path` 形态 + `tag`） |
| 存储方案 | `files` 加列 `tag`，约束 `UniqueConstraint("workspace_id", "tag", name="uq_files_workspace_tag")`（与既有 `uq_files_workspace_uri` 同形）|
| tag 字符集 | 规范化：`strip()` → 纯空白记为 `NULL`；非空必须匹配 `^[A-Za-z0-9._:-]{1,128}$`，否则 **400**。大小写敏感、精确匹配 |
| 前端"只在单文件加 tag" | tag 非空且（拖入目录 **或散文件数 > 1**）→ **整批拦截并提示**；tag 为空时批量照常、不加 tag |
| reprocess / 数据源连接器 | **不改**——tag 只在新建文件时设定 |
| 普通上传撞 tag | 普通 `POST upload` 撞**本 workspace 内**已占 tag → **409**（按约束名区分，见 §4.2）；自动「建/改/移」集中在 upsert |
| 按 tag upsert（新增） | `PUT /service/v1/workspaces/{name}/documents/upsert-by-tag`：显式 `target_path`（完整文件路径）。tag 不存在→建；`existing.uri == target_path`→更新；异 uri→**同一行 move+replace**（保留 `file.id`/`tag`）；目标路径被他文档占→409；返回 `action` |
| 外部删除（新增） | `DELETE /service/v1/workspaces/{name}/documents/by-path`，默认异步软删除 202、可选同步物删 200 |
| 删除统一软删除 | 异步删除入口（内部 `DELETE /files/{id}`、外部 `DELETE by-path`、内部 `delete-path-prefix`）一律：`deleted_at=now()` + `tag=NULL` + 入队清理任务，**同一次 commit**；失败 `rollback`。`background=false` 走原同步物删 |
| 软删后 URI 占用 | 用户已确认接受：软删只释放 tag，不释放 uri；**同 tag + 同路径立即重建返回 409 pending deletion**，直到 worker 物理删除旧行 |
| active 行过滤 | 所有返回文件、替换、预览、父目录校验、检索候选文件 ID 和检索回查默认只看 `deleted_at IS NULL`；worker 物理清理只处理已软删行 |
| 删除任务事务安全 | 新增非提交的 `TaskService.add_task()`；释放 tag + 建任务在同一 session、单次 commit（不给 `create_task` 加 `commit=False` 分支）|

## 3. 关键现状（代码事实）

- **所有"新文件上传"都汇聚到** `ingest_new_file()`（[file_ingest.py:345](../../../openrag/src/openrag/services/file_ingest.py)）；唯一需插入 tag 的新建 chokepoint。现序为 `ensure_directory_path()`（自动建父目录，:458）**先于** 重复 URI 预检查（:461-483）。
- 新上传入口仅两个，均单文件、均调 `ingest_new_file()`：`POST /files/upload`（[files_api.py:348](../../../openrag/src/openrag/api/files_api.py)，内部 JWT）、`POST /service/v1/workspaces/{name}/documents`（[service_api.py:295](../../../openrag/src/openrag/api/service_api.py)，外部 token）。
- `replace_file_content`（[file_ingest.py:589](../../../openrag/src/openrag/services/file_ingest.py)）在**传入的同一 `File` 行**上清旧块/向量、覆盖**同一 `uri`** 的 MinIO 对象、重入队 `process_document`；**不建新行、不改 `uri`、不触碰 `tag`** → upsert 的「更新」「同行移动」分支复用它。
- **单行移动机制现成**：`move_file` 端点（[files_api.py:914](../../../openrag/src/openrag/api/files_api.py)）+ `minio_storage.move_file`（[minio_storage.py:176](../../../openrag/src/openrag/storage/minio_storage.py)）+ `move_document_hierarchy`（[minio_storage.py:324](../../../openrag/src/openrag/storage/minio_storage.py)）→ upsert 同行 move 复用之，提炼为服务层 helper。
- **`TaskService.create_task()` 内部自带 `db.commit()` + `db.refresh()`**（[task_service.py:66-68](../../../openrag/src/openrag/services/task_service.py)）；当前**没有**非提交版本 → 需新增 `add_task()`。
- **删除现状**：外部 `service_api.py` 无任何 `@router.delete`。内部 `DELETE /files/{file_id}`（[files_api.py:741](../../../openrag/src/openrag/api/files_api.py)，JWT，owner/admin write）默认异步建 `TaskType.DELETE_FILE`，`background=false` 同步 `delete_file_with_storage(db, file, workspace)`（[file_deletion.py:82](../../../openrag/src/openrag/services/file_deletion.py)）。前缀删除 `POST /files/delete-path-prefix`（[files_api.py:825](../../../openrag/src/openrag/api/files_api.py)）异步删目录树（**含 tagged 文件**），用 `TaskType.DELETE_PATH_PREFIX`。删除清理全程按 `file.id` / `file.uri` 作用，**无一处按 tag**（[file_deletion.py:109-117](../../../openrag/src/openrag/services/file_deletion.py)）。
- **现有 active 文件路径**（须补 `deleted_at IS NULL` 过滤）：`files_api` 的 `list_files` / 文件详情 / preview；`service_api` 的 by-path / by-tag / replace-by-path / preview-links；`workspace_file_tree` 的 `build_nested_tree` / `get_file_document_by_path` / `list_entries_by_prefix` / `list_direct_children` / `search_documents_by_name`（[workspace_file_tree.py](../../../openrag/src/openrag/services/workspace_file_tree.py)，被 service tree/children/entries/by-path/search-by-name 复用）；`workspace_file_api` / `embed_preview_api` 的预览；检索候选文件 ID 与回查（`retrieval_service` 把 Milvus/ES chunk 命中映射回 `File` 行处）。
- `File` 模型（[file.py](../../../openrag/src/openrag/models/file.py)）**无 tag / deleted_at 字段**；已有 `UniqueConstraint("workspace_id", "uri", name="uq_files_workspace_uri")`（[file.py:175](../../../openrag/src/openrag/models/file.py)，**本就 per-workspace**，新 tag 唯一性沿用同形）；`@validates(...)`（[file.py:84](../../../openrag/src/openrag/models/file.py)）做 NUL 清洗。
- `service_api.py` 可复用 helper：`_token_can_read_workspace`（[service_api.py:228](../../../openrag/src/openrag/api/service_api.py)）、`_document_summary`（[:172](../../../openrag/src/openrag/api/service_api.py)）、`_upload_response_dict`（[:184](../../../openrag/src/openrag/api/service_api.py)）、`require_workspace_for_name` / `assert_token_workspace_permission`。
- 前端 `FileUpload.tsx`：单文件走 `customRequest`（[:305](../../../web/src/components/FileUpload.tsx)），文件夹由 capture `onDrop`（[:273](../../../web/src/components/FileUpload.tsx)）检测 `isDirectory` 后走 `runFolderUpload()`（[:199](../../../web/src/components/FileUpload.tsx)）；`Dragger` `multiple:false`（[:303](../../../web/src/components/FileUpload.tsx)）；两路均调 `filesAPI.upload(...)`（[api.ts:103](../../../web/src/services/api.ts)）。
- DB：生产 PostgreSQL、测试 SQLite；UNIQUE 索引都允许多 NULL（多列唯一约束中含 NULL 的行互不冲突）。Alembic 规范 `YYYYMMDD_NNNN`，当前 head `20260602_0003`（[迁移示例](../../../openrag/alembic/versions/20260602_0003_add_file_document_type.py)）。

## 4. 方案设计

### 4.1 数据模型 + 迁移

- `File` 模型（[file.py](../../../openrag/src/openrag/models/file.py)）：
  - `tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="Per-workspace unique tag (single-file upload only)")`；`"tag"` 加入第 84 行 `@validates` 列表。
  - `deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP, nullable=True, comment="Soft-delete marker; row pending physical cleanup")`。
  - `__table_args__` 追加 `UniqueConstraint("workspace_id", "tag", name="uq_files_workspace_tag")` 与 `Index("idx_files_deleted_at", "deleted_at")`。
- 迁移 `openrag/alembic/versions/20260626_0004_add_file_tag_and_deleted_at.py`（`down_revision="20260602_0003"`）：
  - `op.add_column("files", sa.Column("tag", sa.String(length=128), nullable=True))`
  - `op.add_column("files", sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True))`
  - `op.create_unique_constraint("uq_files_workspace_tag", "files", ["workspace_id", "tag"])`
  - `op.create_index("idx_files_deleted_at", "files", ["deleted_at"])`
  - `downgrade`：逆序 drop。
  - 注：测试 SQLite 经 `Base.metadata.create_all()` 建表（不跑迁移），约束/索引来自模型声明；迁移只需在 PG 跑通。

### 4.2 写入链路（唯一 chokepoint）

`ingest_new_file()`（[file_ingest.py:345](../../../openrag/src/openrag/services/file_ingest.py)）新增关键字参数 `tag: Optional[str] = None`：

- **规范化 + 字符集**：`tag = (tag or "").strip()`；空 → `None`；非空必须匹配 `^[A-Za-z0-9._:-]{1,128}$`，否则 `HTTPException(400)`。
- **查重前置**（Codex #9）：tag 校验与 **workspace 内**查重 `db.query(File).filter(File.workspace_id == workspace.id, File.tag == tag).first()` → 命中即 `HTTPException(409, "Tag already in use")`，**放在 `ensure_directory_path()` 之前**，使 tag 冲突时不残留空目录、不写 MinIO。（软删除行 tag 已为 NULL，天然不参与查重。）
- **父目录 active 校验**：`require_parent_dir=True` 时只接受 `deleted_at IS NULL` 的目录行；自动建目录时，任一祖先路径若已有 `deleted_at IS NOT NULL` 的目录行，返回 `409 pending deletion`，不在正在删除的目录树下创建新文件。`ensure_directory_path()` 的查找与建目录逻辑也应只把 active 目录视为存在。
- 创建 `File(..., tag=tag)`。
- **`IntegrityError` 按约束名区分**（Codex #6）：`db.commit()` 抛 `IntegrityError` 时检查约束名——`uq_files_workspace_tag` → 409「Tag already in use」；`uq_files_workspace_uri` → 维持现有 duplicate-URI 语义；其它 → 通用 409/500。**不可**把所有 `IntegrityError` 一律当 tag 冲突。
- 其余流程（trace、任务、返回值）不变。

### 4.3 两个上传入口（各加一个可选参数透传）

- `POST /files/upload`（[files_api.py:348](../../../openrag/src/openrag/api/files_api.py)）：加 `tag: Optional[str] = Form(default=None)`，透传 `ingest_new_file(..., tag=tag)`。
- `POST /service/v1/workspaces/{name}/documents`（[service_api.py:295](../../../openrag/src/openrag/api/service_api.py)）：同样加 `tag` Form 并透传。
- **不改**：`PUT .../by-path`、`reprocess`、数据源连接器。

### 4.4 检索入口（新增，workspace-scoped）

`GET /service/v1/workspaces/{workspace_name}/documents/by-tag?tag=XXX`：

- 鉴权：`require_workspace_for_name(W)` + `assert_token_workspace_permission(ctx, ws.id, "read")`（无 read → **403**，与现有 by-path 一致）。
- 查询：`db.query(File).filter(File.workspace_id == ws.id, File.tag == tag, File.is_directory.is_(False), File.deleted_at.is_(None)).first()`；为空 → `404`。
- 返回元数据 dict（复用 `_document_summary(f)` + `tag`）。

### 4.5 tag 可见性

- 上传/upsert 响应回显 tag：`_upload_response_dict`（service）、`FileUploadResponse` / `_file_to_upload_response`（内部）加可选 `tag`。
- 文件详情/列表响应 schema 加可选 `tag`（`_document_summary` 同步带 `tag`），供内部 UI 展示。

### 4.6 前端（只在单文件路径加 tag）

- 弹窗表单加可选「唯一标签」`Input`，`useRef` 镜像供事件回调读最新值。
- 单文件 `customRequest`（[FileUpload.tsx:305](../../../web/src/components/FileUpload.tsx)）调 `filesAPI.upload(...)` 时传 `tag.trim() || undefined`。
- **批量守卫**（Codex #7，显式实现）：capture `onDrop`（[FileUpload.tsx:273](../../../web/src/components/FileUpload.tsx)）中，当 `tagRef.current` 非空且（`hasDirectory` **或** `dataTransfer.files.length > 1`）→ `preventDefault()`+`stopPropagation()`+`message.warning(...)` 并 `return`，不发生任何上传。**不依赖** rc-upload「`multiple:false` 一般只取第 1 个」的隐式行为。
- `runFolderUpload()` 不读 tag；上传成功后清空 tag。
- `api.ts` `filesAPI.upload()` 加可选 `tag?: string`，非空时 `formData.append('tag', tag)`。
- 409 → 友好提示「该标签已被占用，请换一个」。i18n 新增 `tag_label` / `tag_conflict` / `tag_batch_blocked`。

### 4.7 删除入口（统一软删除事务）

**事务安全基座**（Codex #1）：
- `TaskService` 新增 **非提交** 的 `add_task(...)`：只 `Task(...)` + `db.add(task)` + 返回，不 `commit()`/`refresh()`；现有 `create_task()` 保持「调用即提交」契约，内部改为 `add_task()` 后再 `commit()`/`refresh()`。
- 新增内部 helper `_release_tag_and_soft_delete(db, file, *, user_id) -> Task`：在同一 session 内 `file.deleted_at = utcnow()`、`file.tag = None`、`TaskService(db).add_task(task_type=DELETE_FILE, file_id=file.id, ...)`，**只调一次 `db.commit()`**；失败 `db.rollback()`（tag/`deleted_at` 一起回滚，旧文件不进入 deleting 状态）。

**外部新增** `DELETE /service/v1/workspaces/{workspace_name}/documents/by-path?path=XXX&background=true`：
- 鉴权：`require_workspace_for_name(W)` + `assert_token_workspace_permission(write)`。
- 定位：`workspace_id==ws.id, uri==validate_path(path), is_directory==False, deleted_at IS NULL`；为空 → 404。
- `background=true`（默认）：`_release_tag_and_soft_delete(...)` → 202 + task_id。
- `background=false`：`delete_file_with_storage(db, row, ws)` 同步物删（行消失）→ 200。
- `user_id` 取 `ws.owner_id`。

**内部 `DELETE /files/{id}`**：`background=true` 分支改用 `_release_tag_and_soft_delete(...)`（已确认内外一致）；`background=false` 不变。

**前缀删除 `POST /files/delete-path-prefix`**：`background=true` 分支必须同事务软删整个 subtree，而不是只记录 prefix：
- 计算 `deleted_before = utcnow()`。
- 查询 `workspace_id==body.workspace_id` 且 `uri == prefix OR uri LIKE prefix + "/%"` 且 `deleted_at IS NULL` 的全部文件/目录行；批量设置 `deleted_at=deleted_before`、`tag=None`。
- `add_task(DELETE_PATH_PREFIX, payload={"path": prefix, "deleted_before": deleted_before.isoformat()})`，上述修改与任务插入**单次 commit**；失败 `rollback`。使前缀删除与单文件删除的 tag 释放语义一致（Codex #4）。
- worker 执行 `DELETE_PATH_PREFIX` 时，物理删除查询必须限定为 `deleted_at IS NOT NULL AND deleted_at <= deleted_before` 的行，并按 URI 深度从深到浅删除。不能只按 prefix 删除当前所有行，否则会误删任务入队后在同 prefix 下新建的 active 文件。
- 因为 `ensure_directory_path()` / 父目录校验不允许写入 pending deletion 的目录树，正常情况下不会出现“软删后又写入同 subtree”的 active 行；worker 侧仍保留 `deleted_at` 过滤作为最后防线。

需在 `service_api.py` import：`delete_file_with_storage` / `TaskService` / `TaskType` / `TaskStatus`。

### 4.8 统一 upsert-by-tag 入口（新增，workspace-scoped）

`PUT /service/v1/workspaces/{workspace_name}/documents/upsert-by-tag`，**显式 `target_path`**（完整文件路径，Codex #10——不由 `file.filename` 隐式派生目标 uri）：

```
PUT /service/v1/workspaces/{workspace_name}/documents/upsert-by-tag
Form: tag=XXX, target_path=/dir/name.pdf, parser_type=auto, create_dirs=false   （body: 新文件 multipart）
```

- 鉴权：`require_workspace_for_name(W)` + `assert_token_workspace_permission(write)`。
- 记 `target_uri = validate_path(target_path)`，`existing = db.query(File).filter(File.workspace_id == ws.id, File.tag == tag, File.is_directory.is_(False), File.deleted_at.is_(None)).first()`（**仅本 workspace**；软删行 tag 已 NULL，天然不命中）。**决策树**：

  | 情形 | 行为 | `action` / 状态码 |
  |---|---|---|
  | `existing is None` | **创建**：`ingest_new_file(..., upload_filename=basename(target_uri), parent=dirname(target_uri), tag=tag, require_parent_dir=not create_dirs)`（目标 uri 被他文档占 → ingest 自身 409） | `created` / 201 |
  | `existing.uri == target_uri` | **更新**：`replace_file_content(db, ws, ws.owner_id, existing, ...)`（保留 id/uri/tag） | `updated` / 200 |
  | `existing.uri != target_uri` | **同一行 move+replace**：见下 | `moved` / 200 |

- **同一行 move+replace 分支**（Codex #2，复用积木、**绝不删行**）：
  1. 预检查 `(W, target_uri)` 是否被**另一文档**（含软删行，uri 仍占用）占用 → 占用则 **409**；目标父目录必须是 active 目录，若父目录 pending deletion → **409 pending deletion**；上述检查**先于**任何存储或 DB 改动。
  2. 不直接串联现有 `move_file` 端点和 `replace_file_content()`，因为二者当前都会在内部执行存储改动和/或多次 `commit()`，无法满足 upsert 的失败语义。应新增专用 helper，例如 `_move_replace_file_content_no_intermediate_commit(...)`，只复用其中的校验、对象 key 计算、chunk 清理等可拆出的原子片段。
  3. 建议顺序：先校验 tag/target_path/parser/文件大小/mime；把新内容写入 `target_uri` 对象（失败则 DB 不变）；在同一 DB 事务内更新同一行的 `uri`、`name`、`size`、`mime_type`、`parser_type`、processing 字段，删除旧 `DocumentChunk` 行，`TaskService.add_task(process_document)`，然后单次 `commit()`；提交失败则 `rollback()` 并 best-effort 删除刚写入的 `target_uri` 对象。
  4. DB commit 成功后，再 best-effort 清理旧 `old_uri` 的原文件对象、旧层级对象与旧向量。清理失败不回滚已提交的新元数据，交给日志/重试/后续清理处理。
  5. 全程保留同一 `file.id` 与 `existing.tag`。
- **安全性**：永不删行、永不释放 tag → **无"删了旧、新没建成就丢文档"窗口**；但 MinIO/Milvus 与 DB 不能跨系统原子提交，方案承诺应限定为“失败时不清 tag、不删除 File 行，尽量保持旧文档或新文档至少一个可达”，并通过先写新对象、后提交 DB、提交后清旧对象来降低破坏性窗口。
- 返回元数据 dict + `action` + `task_id`。
- **`target_path` 语义**（Codex #10）：始终是完整文件路径；创建分支以其 basename 作文件名。不再用泛化 `path` + 上传文件名隐式决定 uri，杜绝「改了上传文件名 → 误触发 move」。
- 复用 `ingest_new_file` / `replace_file_content`（已 import）；新增单行 move helper（提炼自 `move_file`）。

### 4.9 软删除（`deleted_at`）语义

- **写入入口**（全部同事务，§4.7）：内部 `DELETE /files/{id}`、外部 `DELETE by-path`、前缀 `delete-path-prefix` 的 `background=true` 分支 → `deleted_at=now()` + `tag=None` + 入队物理清理任务，单次 commit。`background=false` 直接物删（行消失）。
- **active 行过滤**（§3 已列）：列表 / 详情 / by-path / by-tag / tree / children / entries / search-by-name / service replace-by-path / 预览链接创建 / embed preview / workspace preview / **检索候选 file_id 与检索回查** 一律默认 `deleted_at IS NULL`。检索若先命中 Milvus/ES chunk 再回查 file，必须在回查阶段丢弃 `deleted_at IS NOT NULL` 的行；`_accessible_file_ids()` 也必须只返回 active 文件 ID，且 admin + 指定 workspace 无 active 文件时返回 `[]` 而不是 `None`，避免退化成不带 file_id 过滤的全量检索。**实现计划须逐条审计读、写、预览和检索路径**。
- **效果**：删除入队成功后旧文档对用户/外部 API/检索**立即不可见**，tag **立即可复用**；worker 仅负责物理清理（MinIO/层级/向量/chunks/行）。
- **失败语义**：任务创建前事务失败 → `deleted_at`+tag 一起回滚；任务已建但 worker 失败 → 不恢复 tag、不清 `deleted_at`，任务保持 failure 待重试，旧文件继续逻辑不可见。
- **`uri` 仍占用的边界**（重要）：软删行**保留 `uri`**（供 worker 清理），故 `(workspace_id, uri)` 唯一约束仍占用到物理删除——**同 uri 的裸 `POST upload` 仍会 409**。用户已确认接受：**同 tag + 同路径立即重建也返回 409 pending deletion**，直到旧软删行被 worker 物理删除；同路径刷新应在删除前走 `PUT upsert-by-tag` 的「更新」分支（原地替换、不依赖 uri 释放）。如要让同 uri 即时可重建，需把 `uq_files_workspace_uri` 改为 partial index `WHERE deleted_at IS NULL`（**改动现有约束，本次不做**，除非后续确认需要）。
- tag 查找天然忽略软删行（tag 已 NULL）；uri 检查须包含软删行（uri 仍占用）。
- **URI 查重的 409 区分**：`ingest_new_file` 与 upsert 创建分支的 `(workspace_id, uri)` 查重——命中**软删行** → `409` 且 reason 标注 `pending deletion`（旧行物理删除后即可重试）；命中 **active 行** → 维持现有 `File already exists` 语义。让调用方区分「路径被他文档占用」与「旧文档正在删除中」。

## 5. 边界与不做（Non-goals）

- **不**支持**修改 tag 的值**（改名/重打/清空）——tag 不可变，reprocess 不动 tag。能做：`PUT upsert-by-tag` 建/改/移、`DELETE` 释放 tag。
- 普通 `POST upload` 撞已占 tag 一律 **409**；自动「建/改/移」只在 `PUT upsert-by-tag`（显式 opt-in）。
- 文件唯一性键是**完整 `uri`（含目录）**，裸文件名不是键：同名不同目录本就并存。
- upsert 异路径采用**同一行 move**，**不**做「删旧建新」，故无 tag 丢失窗口；`file.id`/`created_at` 在 move 后**保持不变**（不是新文档）。
- 软删除后 **uri 仍被占用**到物理清理：同 uri 裸上传仍 409；用户已确认同 tag + 同路径立即重建返回 **409 pending deletion**（见 §4.9），同路径刷新用删除前的 upsert 更新分支。
- **不**改 `uq_files_workspace_uri` 为 partial index（本次不让软删 uri 即时可重建）。
- **不**为数据源连接器（批量同步）加 tag。
- **不**新增内部 JWT 的按 tag 检索端点（检索仅外部 token；内部 UI 看文件元数据里的 tag）。
- tag 大小写敏感、精确匹配，仅 `^[A-Za-z0-9._:-]{1,128}$`；**不**做模糊/前缀/Unicode。

## 6. 测试

**后端**（PostgreSQL / SQLite）：
- 带 tag 单文件上传 → `File.tag` 落库；响应回显 tag。
- **不同 workspace 可复用同一 tag**（互不冲突）；同 workspace 重复 tag → 409。
- tag 规范化边界：合法字符集、超长(>128)→400、纯空白→NULL、非法字符（空白内、`/ ? # &`、Unicode 组合）→400、`ABC` vs `abc` 区分。
- `IntegrityError`：并发抢同 `(workspace,tag)` → 一成一 409；并发抢同 `(workspace,uri)` → 命中 uri 语义而非 tag 语义。
- tag 查重在 `ensure_directory_path` 之前：tag 冲突上传不残留空目录。
- `GET /workspaces/{name}/documents/by-tag` 命中 → 元数据 + tag；无 read 权限 → 403；未知 tag → 404；命中软删行 → 404（被 `deleted_at` 过滤）。
- `DELETE by-path`（异步）→ 文档**立即**从列表/by-path/by-tag/检索消失（`deleted_at` 过滤）、tag **立即**可复用；旧行物理清理异步、按 id/uri 作用不伤新行。
- **任务创建失败时 tag 不被提前释放**（事务回滚）：模拟 `add_task`/commit 失败 → `deleted_at`/tag 未变。
- 前缀删除含 tagged 文件 → 命中行 `deleted_at`+`tag=None` 同事务、对读不可见、tag 释放。
- 前缀删除任务入队后，在同 prefix 下后来出现的 active 行不会被 worker 物理删除；worker 只删除 `deleted_at IS NOT NULL AND deleted_at <= payload.deleted_before` 的行。
- pending deletion 的目录不能作为上传 / upsert / create_dirs 的 active 父目录；命中时返回 409 pending deletion。
- `upsert-by-tag`：tag 不存在→`created`；`target_path==existing.uri`→`updated`（同 id/uri/tag）；异 uri→`moved`（**同 file.id 不变**、tag 不变、旧路径消失、新路径有新内容）；目标路径被他文档占→409；**新建/移动失败时原 tag 与原文档可达性不丢**；无 write→403。
- `upsert-by-tag` 异 URI move 分支模拟 DB commit 失败 / MinIO 写失败：不清 tag、不删 File 行；若已写新对象则 best-effort 清理，旧行仍可通过原 tag 找到。
- 同 uri 裸 `POST upload`（目标被软删行占）→ 仍 409（uri 未释放）；用户已确认同 tag + 同路径立即重建 → 409 pending deletion。
- 软删文件的 by-path / by-tag / replace-by-path / preview-link / embed preview / workspace preview 均返回 404 或不可见。
- 检索：`_accessible_file_ids()` 不返回软删文件 ID；Milvus/ES 命中软删文件 chunk 时，回查阶段丢弃该结果；admin + 指定 workspace 无 active 文件时返回空结果而不是扩大为全量检索。

**前端**（`FileUpload`）：
- 单文件 + 填 tag → `filesAPI.upload` 收到 tag。
- tag 非空 + 拖目录 / 多散文件 → 被拦截、不上传、出现 warning。
- tag 为空拖文件夹/多文件 → 批量正常、不带 tag（不回归）。

## 7. 影响文件清单

- `openrag/src/openrag/models/file.py`（`tag` 列 + `deleted_at` 列 + `@validates` + `uq_files_workspace_tag` + `idx_files_deleted_at`）
- `openrag/alembic/versions/20260626_0004_add_file_tag_and_deleted_at.py`（新建迁移）
- `openrag/src/openrag/services/task_service.py`（新增非提交 `add_task()`；`create_task()` 改为复用之）
- `openrag/src/openrag/services/file_ingest.py`（`ingest_new_file` 加 `tag`：字符集正则 + workspace 内查重前置于建目录 + 按约束名区分 `IntegrityError`）
- `openrag/src/openrag/api/files_api.py`（`/upload` 加 `tag` Form + 回显；`DELETE /files/{id}` 异步分支改用软删除 helper；`delete-path-prefix` 异步分支批量软删+释放 tag 并写入 `deleted_before`；list/详情/preview 读路径加 `deleted_at IS NULL`）
- `openrag/src/openrag/api/service_api.py`（`/documents` 加 `tag` Form；新增 `GET /workspaces/{name}/documents/by-tag`、`PUT /workspaces/{name}/documents/upsert-by-tag`、`DELETE /workspaces/{name}/documents/by-path`；`replace-by-path`、`preview-links`、`_upload_response_dict`/`_document_summary` 处理 active 行和 tag；相关 import）
- `openrag/src/openrag/services/workspace_file_tree.py`（`build_nested_tree`/`get_file_document_by_path`/`list_entries_by_prefix`/`list_direct_children`/`search_documents_by_name` 加 `deleted_at IS NULL`）
- `openrag/src/openrag/retrieval/retrieval_service.py`（`_accessible_file_ids()` 只返回 active 文件；chunk→file 回查阶段过滤 `deleted_at IS NOT NULL`；admin 指定 workspace 无 active 文件时返回空结果）
- `openrag/src/openrag/worker/task_worker.py` / `openrag/src/openrag/services/file_deletion.py`（`DELETE_PATH_PREFIX` 物理清理只删除 payload `deleted_before` 之前已软删的 subtree 行，避免误删后来 active 文件）
- `openrag/src/openrag/api/workspace_file_api.py` / `openrag/src/openrag/api/embed_preview_api.py`（预览路径过滤软删文件）
- 删除释放 tag 的内部 helper（`_release_tag_and_soft_delete`，置于 `file_deletion.py` 或 `file_ingest.py`，供内外删除入口复用）
- upsert move+replace 专用 helper（置于服务层，避免直接串联现有带中间 commit 的 `move_file` / `replace_file_content`）
- 文件详情/列表响应 schema（加可选 `tag`）
- `web/src/components/FileUpload.tsx`（tag 输入 + 显式多文件守卫 + 透传 + 清空 + 409 提示）、`web/src/services/api.ts`（`upload()` 加可选 `tag`）、`web/src/i18n/locales/{zh,en}.json`
- 相应后端与前端测试文件

## 8. Codex 评审结论（来自 Codex，2026-06-26）

### 8.1 总体结论

方案主体可行：把 `tag` 放在 `files` 表、用全局唯一约束兜底、在 `ingest_new_file()` 这一新建 chokepoint 透传、再补一个 service-token 的 `GET by-tag`，这些方向和现有代码结构匹配，改动面也基本可控。

但不建议按当前方案原样直接进入实现。主要风险集中在两个地方：一是“异步删除前同步释放 tag”的事务边界，二是 `upsert-by-tag` 的异路径 move 分支把一个外部看起来像 upsert 的动作实现成“删旧再建新”，数据损坏窗口偏大。建议先收敛 MVP，再逐步加破坏性能力。

### 8.2 主要风险与遗漏 case

1. **异步删除先释放 tag 需要和任务创建放在同一个 DB 事务里。** `TaskService.create_task()` 内部会 `commit()`；应新增不自动提交的 `add_task(...)`，由删除 helper 在同一 session 内 `file.tag=None` + `add_task(...)` 后只 `commit()` 一次，失败 `rollback()`。
2. **`upsert-by-tag` 的 move 分支存在较大的数据损坏窗口。** 不应「先清旧 tag + 排删旧行 + 建新行」；更稳的是抽一个同一行 helper（从 `move_file` 与 `replace_file_content` 提炼），在同一 `File` 行上移动 `uri/name` 并替换内容，始终保留同一 `file.id` 与 `file.tag`。
3. **全局唯一 tag 与“防探测”天然张力；改为 `(workspace_id, tag)` 唯一。** 模型/迁移用 `uq_files_workspace_tag`，所有查重/查找/读取限定在 workspace 内，`GET by-tag` 改 workspace-scoped。
4. **内部前缀/目录删除会碰到 tagged 文件；用 `deleted_at` 逻辑删除统一语义。** 所有异步删除入口同事务设置 `deleted_at=now()` + `tag=None` + 入队任务；所有读路径默认过滤 `deleted_at IS NULL`。
5. **tag 规范化过宽；采用 `^[A-Za-z0-9._:-]{1,128}$`。** 先 `strip()`、纯空白转 NULL、非空必须匹配正则否则 400。
6. **`IntegrityError` 兜底不能泛化成 tag 冲突；按约束名区分。** `uq_files_workspace_tag` → tag 占用；`uq_files_workspace_uri` → uri 语义；其它 → 通用。
7. **前端多散文件拖拽守卫需显式实现。** 在拖拽捕获阶段显式检查 `dataTransfer.files.length > 1` 或 entries 数量；tag 非空 + 多散文件 → 整体拦截。
8. **即时 tag 复用的旧文档搜索窗口由第 4 点 `deleted_at` 覆盖。** 检索回查阶段必须丢弃 `deleted_at IS NOT NULL`。
9. **重复 tag 预检查前置到自动建目录之前。** tag 格式校验 + workspace 内查重不依赖 `path`，可在 `ensure_directory_path()` 前完成，冲突时不留空目录。
10. **`upsert-by-tag` 的 `path` 语义易误用；改为显式 `target_path`（完整文件路径），不由 `file.filename` 隐式决定 URI。**

### 8.3 建议实施顺序（Codex 原议）

1. 第一阶段：`files.tag` + 唯一约束 + tag 格式校验 + 两个 POST 透传 + `GET by-tag` + 回显；普通重复 409；暂不做 move。
2. 第二阶段：删除即时释放（同事务）；明确前缀删除 tag 释放语义。
3. 第三阶段：upsert 先 create/update same URI；异 URI 用同一行 move+replace。

### 8.4 Codex 建议补充测试

见 §6（已并入）：任务插入失败不提前释放 tag；前缀删除含 tagged 文件的释放语义；upsert 异 URI 失败不丢原 tag/原文档；并发重复 tag 与 uri 分别命中正确语义；tag 规范化边界；前端 tag 非空多文件拦截。

### 8.5 处置结论（2026-06-28，用户确认）

**Codex 全部 10 条均采纳**，并已落实到上文 §1–§7：

| # | 处置 |
|---|---|
| 1 事务边界 | ✅ 采纳——新增非提交 `add_task()` + `_release_tag_and_soft_delete` 单事务（§4.7）|
| 2 move 损坏窗口 | ✅ 采纳 Codex「同一行 move+replace」方案（§4.8），保留 `file.id`/`tag`，无丢失窗口 |
| 3 唯一性 | ✅ 采纳——改 `(workspace_id, tag)` 内唯一，`GET by-tag` workspace-scoped（§2/4.1/4.4/4.8）|
| 4 `deleted_at` 软删除 | ✅ 采纳——统一软删除 + 读路径过滤（§4.7/4.9）。代价：读路径横切面较大，须逐条审计 |
| 5 tag 字符集 | ✅ 采纳 `^[A-Za-z0-9._:-]{1,128}$`（§4.2）|
| 6 `IntegrityError` 按约束名 | ✅ 采纳（§4.2）|
| 7 前端显式多文件守卫 | ✅ 采纳（§4.6，原 spec 已含 `>1` 判断，明确不依赖 rc-upload 隐式行为）|
| 8 搜索窗口 | ✅ 由第 4 点 `deleted_at` 覆盖（§4.9 检索回查过滤）|
| 9 查重前置 | ✅ 采纳（§4.2）|
| 10 显式 `target_path` | ✅ 采纳（§4.8）|

**收敛差异**：Codex 第 2 点首选「第一版不做 move」，本方案据用户决策直接采用其「同一行 move+replace」备选（一步到位、对外即 `moved`）。**新增边界**（§4.9）：软删除仅释放 tag、不释放 uri，同 uri 裸上传仍 409，同路径刷新走 upsert 更新分支。

### 8.6 Codex 二次审查追加（来自 Codex 审查，2026-06-28）

Claude Code 根据 §8.1–§8.5 更新方案后，Codex 进行二次审查。主体方案可进入实现，但为避免后续反复审查，本轮补充 4 个必须落实的硬边界，已并入 §2、§4.2、§4.7、§4.8、§4.9、§6、§7：

1. **异步前缀删除不能误删后来新建的 active 文件。** 前缀删除入队时必须同事务软删整个 subtree，并在任务 payload 写入 `deleted_before`；worker 只物理删除 `deleted_at IS NOT NULL AND deleted_at <= deleted_before` 的行。
2. **软删除目录不能继续作为写入父目录。** `ensure_directory_path()`、`require_parent_dir`、upsert/create_dirs 的父目录判断都只认 active 目录；pending deletion 父目录返回 `409 pending deletion`。
3. **upsert 的 move+replace 不能直接串联现有 `move_file` 与 `replace_file_content()`。** 需要专用 helper，避免中间 commit；采用“先写新对象、单次 DB 提交元数据与任务、提交后 best-effort 清旧对象/旧向量”的顺序，承认 MinIO/Milvus 与 DB 不能跨系统原子提交。
4. **`deleted_at` 过滤范围扩大到所有 active 文件路径。** 除列表/详情/tree/search 外，还包括 service replace-by-path、preview-links、workspace/embed preview、检索候选 file_id 与检索回查；`_accessible_file_ids()` 不得把软删文件纳入候选。

用户已确认接受额外约束：软删除只释放 tag、不释放 uri；**同 tag + 同路径立即重建返回 `409 pending deletion`**，直到 worker 完成物理删除旧行。

### 8.7 二次审查处置（2026-06-28，Claude Code 复核）

复核 §8.6 四条，**均可行且已并入正文**——已用代码事实验证落点存在：`_accessible_file_ids()`（[retrieval_service.py:695](../../../openrag/src/openrag/retrieval/retrieval_service.py)，检索候选过滤）、`_delete_path_prefix_task`（[task_worker.py:471](../../../openrag/src/openrag/worker/task_worker.py)，前缀删除 worker）、`Task.payload`（[task.py:170](../../../openrag/src/openrag/models/task.py)，可塞 `deleted_before`）、`move_file`/`minio_storage.move_*`。

| §8.6 | 处置 | 落点 |
|---|---|---|
| 1 前缀删除 `deleted_before` 水位 | ✅ 采纳 | §4.7（入队同事务软删 subtree + payload 写水位；worker 只删 `deleted_at IS NOT NULL AND deleted_at <= deleted_before`）、§6、§7（`task_worker.py`）|
| 2 软删目录不作写入父目录 | ✅ 采纳 | §4.2（`ensure_directory_path`/`require_parent_dir` 只认 active 目录，pending → 409 pending deletion）|
| 3 upsert move 专用 helper | ✅ 采纳，并**纠正**前稿"直接串联 `move_file`+`replace_file_content`" | §4.8（先写新对象 → 单次 DB 提交元数据/任务 → 提交后 best-effort 清旧对象/旧向量；承认跨系统非原子）|
| 4 active 过滤扩面 | ✅ 采纳 | §4.9/§3/§7（含 service replace-by-path、preview-links、workspace/embed preview、`_accessible_file_ids` 的 `[]`-非-`None` 退化防护、检索回查）|

**复核补充**：URI 查重命中软删行 → `409 pending deletion`、命中 active 行 → `File already exists`，二者错误语义区分（§4.9）。

**范围提示**：连同 §8.6，本功能已从"加一列 + 几个端点"扩展为含**软删除子系统**（横切 worker、检索、预览、目录写入）。强烈建议按 §8.3 **分三阶段**实施，每阶段独立可测，避免一次性大改难以验证。
