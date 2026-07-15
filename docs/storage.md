# OpenRag 存储现状说明

本文档用于说明当前 OpenRag 项目会持久化哪些数据、这些数据分别存储在哪里，以及哪些解析和检索过程中的中间产物目前不会被保存。

## DocumentBlock 全量列表

`DocumentBlock` 定义在 `openrag/src/openrag/parsers/base.py`，是文档解析器输出给切块流程的内存对象。一次 `parser.parse(file_path)` 会返回一个 `list[DocumentBlock]`，这个列表就是“DocumentBlock 全量列表”。

单个 `DocumentBlock` 表示解析器识别出来的一个内容块。它可能是一段正文、标题、表格、图片、代码块，或者 PDF 版面中的某个区域。主要字段包括：

| 字段 | 含义 |
| --- | --- |
| `text` | 解析出的文本内容。 |
| `page` | 页码。 |
| `offset` | 字符偏移量。 |
| `bbox` | PDF 等版面文件中的坐标，格式为 `(x1, y1, x2, y2)`。 |
| `block_type` | 内容块类型，如 `text`、`table`、`image`、`code`。 |
| `level` | 标题层级，`0` 表示正文，`1-6` 表示不同级别标题。 |
| `block_id` | 解析器内部生成的稳定逻辑块 ID。 |
| `char_start` / `char_end` | 在规范化全文字符流中的起止位置，采用半开区间。 |
| `image` | 图片二进制数据，可选。 |
| `table_data` | 表格结构化数据，可选。 |
| `layout_type` | 布局类型，如 `title`、`text`、`table`、`figure`、`list`。 |
| `confidence` | OCR 置信度，可选。 |
| `language` | 检测到的语言，可选。 |
| `metadata` | 解析器补充的其他元数据。 |

当前项目中，`list[DocumentBlock]` 只在 `DocumentProcessor.process_document()` 的内存流程中使用：先由解析器生成，再传给 `ChunkEngine.chunk()` 生成 chunk。它本身不会作为完整解析快照写入 PostgreSQL、MinIO、Milvus 或 Elasticsearch。

## PostgreSQL 中保存的数据

PostgreSQL 是系统的权威元数据存储，主要保存用户、工作区、文件、工作区权限、任务、切片元数据等结构化数据。

| 数据类型 | 表/模型 | 保存内容 |
| --- | --- | --- |
| 用户 | `users` | 用户名、邮箱、密码哈希、姓名、启用状态、管理员标记。 |
| 工作区 | `workspaces` | 工作区名称、slug、所有者、任务并发限制、存储配额、优先级策略。 |
| 工作区成员 | `workspace_members` | 用户和工作区的成员关系，以及 `read` / `write` 权限角色。 |
| 团队 | `teams`、`team_members` | 团队信息、团队成员、团队角色。 |
| 角色权限 | `roles`、`role_workspace_permissions`、`user_roles` | 系统角色、角色对工作区的权限、用户和角色的绑定关系。 |
| 文件/目录元数据 | `files` | 逻辑路径 `uri`、文件名、所有者、工作区、是否目录、文件大小、MIME 类型、parser 类型、处理状态、L0/L1/L2 路径、chunk 数、token 数。 |
| 切片元数据 | `document_chunks` | `chunk_id`、`chunk_index`、对象存储路径、文本预览、页码、层级、bbox、offset、source block/char span 等。 |
| 后台任务 | `tasks` | task ID、工作区、用户、文件、任务类型、队列、优先级、状态、进度、重试次数、worker、结果、错误、payload。 |
| 分享链接 | `share_links` | 分享 token、密码哈希、过期时间、访问次数限制、当前访问次数。 |
| 服务令牌 | `service_tokens`、`service_token_workspaces` | 机器访问令牌、创建人、撤销时间、授权工作区和权限。 |
| 审计日志 | `audit_logs` | 用户操作、资源类型、资源 ID、详情、IP 地址、创建时间。 |

`file_permissions` 文件级 ACL 已由 [R-02 文件级 ACL 下线方案](./R-02文件级ACL下线方案.md) 下线完成，不属于当前代码的活动数据模型；文件访问统一由工作空间 `read` / `write` 权限控制。实施前生产核查结果为 `0` 行，下线迁移按零记录保护路径删除该表。

需要注意的是，`document_chunks.text_preview` 只是 chunk 文本预览，不是完整解析结果。完整 chunk 正文主要保存在对象存储中的 L2 chunk 文件里，同时也会写入 Milvus 和可选的 Elasticsearch 索引中。

## MinIO 中保存的数据

MinIO 是当前生产链路中的主要对象存储。逻辑上，系统会以 `workspace.slug` 作为 bucket 名；如果开启了单 bucket 配置，则会通过 `STORAGE__BUCKET` 和 `STORAGE__PREFIX` 把逻辑 bucket 和对象 key 映射到实际物理位置。

| 数据类型 | 对象位置 | 保存内容 |
| --- | --- | --- |
| 原始上传文件 | bucket 为 `workspace.slug`，object 为 `file.uri` | 用户上传的原始文件字节。 |
| L0 摘要 | `hierarchy/<file_uri>.abstract.md` | 文档或目录的 L0 abstract 文本。 |
| L1 概览 | `hierarchy/<file_uri>.overview.md` | 文档或目录的 L1 overview 文本。 |
| L2 chunk 正文 | `hierarchy/<file_uri>/chunks/NNNN.md` | 每个 chunk 的完整正文，按 `0000.md`、`0001.md` 等编号保存。 |

`files.l0_path`、`files.l1_path`、`files.l2_path` 会记录 L0/L1/L2 对象的 path-style URL 或目录 URL 前缀。`document_chunks.object_key` 和 `document_chunks.object_url` 会记录单个 L2 chunk 的对象 key 和访问 URL。

文件删除或重新处理时，系统会清理对应的源文件对象、L0/L1/L2 层级对象、`document_chunks` 数据行，以及相关 Milvus 向量和 Elasticsearch 全文索引。

## 本地文件系统回退存储

`HierarchyStorage` 提供本地文件系统回退存储，默认路径为：

```text
openrag/storage/hierarchies
```

也可以通过环境变量 `HIERARCHY_STORAGE_PATH` 指定其他目录。它的布局与 MinIO 中的层级对象类似：

- `<stem>.abstract.md`
- `<stem>.overview.md`
- `<stem>/chunks/NNNN.md`

当前 worker 正常会传入 `MinioStorage`，所以生产链路优先使用 MinIO。本地文件系统回退更多用于测试、开发环境，或兼容没有 MinIO 的场景。

worker 执行处理任务时，还会把源文件临时下载到系统临时目录，处理完成后删除。这个临时文件不属于持久化存储。

## Milvus 中保存的数据

Milvus 保存向量检索需要的数据。当前主要有两个 collection。

| Collection | 保存内容 | 用途 |
| --- | --- | --- |
| `openrag_chunks` | L2 chunk 向量，以及 `chunk_id`、`file_id`、`text`、`page`、`level`、`block_type` 等标量字段。 | 普通 chunk 语义检索。 |
| `openrag_layers` | L0/L1 层级向量，以及 `layer_row_id`、`file_id`、`layer`、`text` 等标量字段。 | 上下文检索或分层检索中的 L0 粗筛和 L1 辅助检索。 |

PostgreSQL 不保存 embedding 向量本体，只保存与 Milvus 主键对齐的 `chunk_id` 和相关元数据。向量本体保存在 Milvus 中。

## Elasticsearch 中保存的数据

Elasticsearch 是可选全文索引。只有在配置启用且连接可用时，处理链路才会写入 ES。

| 数据类型 | 存储位置 | 保存内容 |
| --- | --- | --- |
| chunk 全文索引 | 每个工作区一个 index，例如 `openrag_ws_<slug>_chunks` | `chunk_id`、`file_id`、`workspace_id`、`workspace_slug`、`content`，以及写入时附带的 `doc_type_kwd`、`content_with_weight`、`content_ltks`、`content_sm_ltks`、`mom_with_weight` 等字段。 |

检索时，Elasticsearch 主要用于给候选 chunk 计算 BM25 或全文匹配分数，再与向量分数进行融合。

## 当前不会持久化的中间产物

以下内容目前不会作为独立对象保存：

- `parser.parse()` 返回的 `list[DocumentBlock]` 全量列表。
- 解析器的原始返回结构，例如 OCR、layout、table 的完整原始识别结果。
- 解析阶段的完整调试快照。
- 检索过程中的粗排 topN、ES 融合前后 topN、rerank topN 快照。
- 每次上传、处理或查询的 trace/span 明细。
- embedding 向量在 PostgreSQL 中的副本。

也就是说，当前系统主要保存“最终可检索、可展示、可管理”的文件、层级、chunk、向量和索引数据；但不会保存“解析器中间态”和“检索排序过程快照”。这些未保存的部分，正是后续 Trace 体系可以补充的内容。
