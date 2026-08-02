# OpenRag 项目技术详解

> 面向小白的完整项目介绍，涵盖架构、功能模块、技术栈与设计思路。

---

## 目录

1. [项目概述](#1-项目概述)
2. [整体架构](#2-整体架构)
3. [身份与组织管理](#3-身份与组织管理)
4. [文档与目录管理](#4-文档与目录管理)
5. [文档处理流水线](#5-文档处理流水线)
6. [检索功能](#6-检索功能)
7. [权限系统](#7-权限系统)
8. [任务调度与后台Worker](#8-任务调度与后台worker)
9. [外部数据源连接器](#9-外部数据源连接器)
10. [技术栈总结](#10-技术栈总结)

---

## 1. 项目概述

**OpenRag** 是一个面向企业的 **B2B SaaS 智能文档管理与检索系统**。它的核心能力是：

- 你上传一堆文档（PDF、Word、Excel、PPT、Markdown、网页等）
- 系统自动解析、分块、向量化，存入向量数据库
- 之后你可以用自然语言搜索，系统找到最相关的文档片段返回给你

打个比方：它就像企业内部的"ChatGPT + 知识库"，只不过搜索的不是全网内容，而是你企业自己的文档。

### 解决什么问题？

传统企业里，文档散落在各个角落——共享文件夹、邮件附件、SharePoint、Google Drive……找一份合同或技术方案可能要翻半天。OpenRag 把这些文档统一管理，用 AI 语义搜索代替关键词搜索，极大提升找文档的效率。

---

## 2. 整体架构

OpenRag 采用 **前后端分离 + 微服务化** 的架构，主要分为以下几个部分：

```
┌──────────────────────────────────────────────────────┐
│                    前端 (React)                        │
│              localhost:3000 / :5173                   │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP/REST
┌──────────────────────▼───────────────────────────────┐
│              FastAPI 后端 (run_api.py)                 │
│              localhost:8000                            │
│  ┌──────────┬──────────┬──────────┬──────────────┐   │
│  │ 用户认证  │ 工作空间  │  文件管理  │  检索服务    │   │
│  │ 权限管理  │ 团队管理  │  分享链接  │  任务管理    │   │
│  └──────────┴──────────┴──────────┴──────────────┘   │
└──────────────────────┬───────────────────────────────┘
                       │
     ┌─────────────────┼─────────────────┐
     ▼                 ▼                  ▼
┌─────────┐    ┌─────────────┐    ┌──────────┐
│PostgreSQL│    │   Milvus    │    │  MinIO   │
│  元数据  │    │  向量数据库  │    │ 对象存储  │
└─────────┘    └─────────────┘    └──────────┘
                      │
              ┌───────▼───────┐
              │ Elasticsearch │  ← 可选，全文检索增强
              └───────────────┘

┌──────────────────────────────────────────────────────┐
│              Worker 进程 (run_worker.py)               │
│         后台处理文档：解析 → 分块 → 向量化 → 存储      │
└──────────────────────────────────────────────────────┘
```

### 核心组件说明

| 组件 | 作用 | 类比 |
|------|------|------|
| **FastAPI** | Web 框架，处理 HTTP 请求，提供 REST API | 餐厅的服务员 |
| **PostgreSQL** | 存储所有"元数据"（用户、文件信息、权限、任务状态等） | 档案室的目录卡片 |
| **Milvus** | 向量数据库，存文档片段的向量，做相似度搜索 | 图书馆的索书号系统 |
| **MinIO** | 对象存储（兼容 S3），存原始文件和生成的层级文件 | 仓库的货架 |
| **Elasticsearch** | 全文搜索引擎（可选），做关键词匹配 | 书的附录索引 |
| **Worker** | 后台进程，异步处理文档（避免用户上传后干等） | 厨房的后厨 |

---

## 3. 身份与组织管理

### 3.1 用户系统 (`User`)

**技术实现**：基于 JWT（JSON Web Token）的无状态认证。

**核心流程**：

```
注册 → 密码 bcrypt 哈希 → 存入 PostgreSQL
登录 → 验证密码 → 签发 JWT Token（有效期 720 分钟）
请求 → 携带 Token（Authorization: Bearer xxx）→ 后端验证签名 → 获取用户身份
```

**数据模型** (`src/openrag/models/user.py`)：
- `username`：用户名（全局唯一）
- `email`：邮箱（全局唯一）
- `password_hash`：密码哈希（bcrypt，12 轮加密）
- `full_name`：全名
- `is_active`：是否激活（可禁用账户）
- `is_admin`：是否为系统管理员（超级权限）

**安全措施**：
- 密码使用 bcrypt 哈希存储，不可逆
- JWT 使用 HS256 算法签名，有过期时间
- 密码长度限制 72 字节（bcrypt 的上限）
- 支持令牌过期检测、签名验证、格式校验

### 3.2 工作空间 (`Workspace`)

**解决的问题**：多租户隔离。不同部门/客户的数据需要完全隔离，A 公司的员工不能看到 B 公司的文档。

**数据模型** (`src/openrag/models/workspace.py`)：
- `name`：工作空间名称（全局唯一）
- `slug`：URL 友好的标识符（如 `engineering-team`）
- `owner_id`：创建者（拥有者）
- `max_concurrent_tasks`：最大并发处理任务数（默认 10）
- `max_storage_bytes`：最大存储空间（默认 10GB）
- `priority_strategy`：任务优先级策略（如 `file_size` 按文件大小）

**成员角色**：
- `read`：只读（可以看文档、搜索）
- `write`：读写（可以上传、修改、删除文档）

### 3.3 团队 (`Team`)

**解决的问题**：方便批量管理权限。比如"研发部"有 30 个人，给每个人单独授权太麻烦，直接给"研发部"这个团队授权即可。

**数据模型** (`src/openrag/models/team.py`)：
- `name`：团队名称（全局唯一）
- `owner_id`：团队创建者
- `workspace_id`：所属工作空间
- 团队成员有三级角色：`owner`（拥有者）> `admin`（管理员）> `member`（普通成员）

### 3.4 角色系统 (`Role`)

**解决的问题**：超越简单的 read/write 二元权限。角色可以定义"在某工作空间拥有某权限"，然后批量分配给用户。

**数据模型** (`src/openrag/models/role.py`)：
- `Role`（角色表）：`name`（显示名）、`role_code`（系统唯一码）
- `RoleWorkspacePermission`（角色-工作空间权限）：定义角色在哪个工作空间有 read 或 write 权限
- `UserRole`（用户-角色关联）：把角色分配给用户

**典型用法**：
```
创建角色 "审计员"，权限 read → 分配给 3 个用户
→ 这 3 个人对所有工作空间的文档都是只读的
```

---

## 4. 文档与目录管理

### 4.1 目录结构

OpenRag 用 **虚拟目录** 管理文件，类似操作系统的文件夹系统：

```
/                              ← 根目录（虚拟）
├── contracts/                 ← 虚拟目录
│   ├── 2024-Q1-contract.pdf   ← 文件
│   └── 2024-Q2-contract.docx  ← 文件
├── technical-docs/
│   ├── architecture.md
│   └── api-spec.json
└── reports/
    └── annual-report.xlsx
```

**关键设计**：
- 目录是数据库中的记录（`is_directory=True`），不是 MinIO 中的真实文件夹
- 文件通过 `uri` 字段表示逻辑路径（如 `/contracts/2024-Q1-contract.pdf`）
- 文件有 `parent_id` 自引用外键，形成树形结构
- 支持按路径前缀浏览（懒加载目录树）

### 4.2 文件上传流程

```
用户上传文件
    │
    ▼
校验权限（write 权限）→ 校验文件大小（≤100MB）→ 校验文件类型
    │
    ▼
存入 MinIO 对象存储（bucket = 工作空间 slug，key = 文件 uri）
    │
    ▼
在 PostgreSQL 创建文件元数据记录（uri, name, owner_id, workspace_id...）
    │
    ▼
创建处理任务（process_document）→ Worker 异步处理
    │
    ▼
返回文件元数据 + 任务 ID
```

### 4.3 文件操作

| 操作 | 说明 | 权限要求 |
|------|------|----------|
| 上传文件 | 上传到指定工作空间和目录 | write |
| 列出文件 | 分页、支持按文件名/类型/上传人/处理状态/时间范围筛选 | read |
| 获取文件内容 | 流式返回 MinIO 中的原始文件（浏览器预览） | read |
| 获取文件预览 | Office/文本文件转 HTML 或纯文本（模态窗预览） | read |
| 移动/重命名 | 修改文件 URI，同步更新 MinIO 和切片对象键 | write |
| 删除文件 | 异步清理 MinIO + Milvus + ES + 数据库 | write |
| 删除目录 | 按路径前缀级联删除所有子文件 | write |
| 创建目录 | 创建虚拟目录记录 | write |
| 重新处理 | 清除已有处理结果，重新跑解析→分块→向量化流程 | write |

### 4.4 支持的文件格式

```
PDF (.pdf)        Word (.docx, .doc)     Excel (.xlsx, .xls)
PowerPoint (.pptx, .ppt)    Markdown (.md)    HTML (.html, .htm)
纯文本 (.txt, .py, .js)    JSON (.json, .jsonl)    CSV (.csv)
EPUB (.epub)
```

---

## 5. 文档处理流水线

这是 OpenRag 最核心的部分。每当你上传一个文档，系统会在后台执行以下 7 个步骤：

### 步骤 1：解析（Parsing）

**技术**：使用 **RAGFlow deepdoc** 解析引擎 + 自研适配器。

**解决的问题**：不同格式的文件（PDF、Word、Excel...）需要被提取成统一的"文本块"数据结构。

**解析器工厂** (`src/openrag/parsers/factory.py`)：根据文件扩展名自动选择合适的解析器。

```
.pdf  → PDFParserAdapter     → RAGFlow pdf_parser（支持 Docling/MinerU/PaddleOCR 多种引擎）
.docx → DocxParserAdapter    → RAGFlow docx_parser
.xlsx → ExcelParserAdapter   → RAGFlow excel_parser
.pptx → PptParserAdapter     → RAGFlow ppt_parser
.md   → MarkdownParserAdapter → RAGFlow markdown_parser
.html → HtmlParserAdapter    → RAGFlow html_parser
.txt  → TxtParserAdapter     → RAGFlow txt_parser
.json → JsonParserAdapter    → RAGFlow json_parser
.epub → EpubParserAdapter    → RAGFlow epub_parser
```

**RAGFlow 的 PDF 解析能力**尤其强大，支持：
- **Docling 解析器**：IBM 开源的文档解析工具，布局识别准确
- **MinerU 解析器**：深度识别 PDF 结构
- **PaddleOCR 解析器**：中文 OCR 效果好
- 视觉布局识别（Layout Recognizer）：识别标题、正文、表格、图片区域
- 表格结构识别（Table Structure Recognizer）

每个解析器输出的都是统一的 `DocumentBlock` 列表，包含：
- `text`：文本内容
- `page`：页码
- `level`：层级（0=正文, 1=一级标题, 2=二级标题...）
- `block_type`：块类型（text, title, table, image...）
- `bbox`：在页面上的位置坐标（x0, y0, x1, y1）

### 步骤 2：分块（Chunking）

**技术**：自研分块引擎 + RAGFlow 语义合并算法。

**解决的问题**：文档可能很长（几百页），需要切成合适大小的片段才能做向量检索。

**三种分块策略** (`src/openrag/chunking/chunk_engine.py`)：

| 策略 | 方法 | 适用场景 |
|------|------|----------|
| **语义分块** (semantic) | 基于 Token 数合并，参考 RAGFlow 的 naive merge 算法 | 大多数文档（默认） |
| **段落分块** (paragraph) | 按 `\n\n` 双换行切分，保持段落完整 | 结构清晰的纯文本 |
| **固定大小** (fixed_size) | 按字符数等长切分，带 overlap | 简单的平文本 |

**参数可配置**（环境变量）：
- `CHUNK_SIZE`：每个块的最大 Token 数（默认 128）
- `CHUNK_OVERLAP`：块与块之间的重叠 Token 数（默认 50）
- `CHUNK_METHOD`：分块方法（semantic / paragraph / fixed_size）
- `MIN_CHUNK_TOKENS`：最小块 Token 数，太小的块会被合并

### 步骤 3：构建 L0/L1/L2 三层文档层级

**技术**：自研层级构建器（`DocumentHierarchyBuilder`），灵感来自 **OpenViking**（火山引擎开源的长上下文检索框架）。

**解决的问题**：传统 RAG 直接搜切片，但用户可能需要先看文档摘要（这篇文档在讲什么），再定位到具体章节，最后看原始切片。这就是"三层检索"的思想。

```
L0（摘要层） ~100 tokens
    ↓ 提供文档的鸟瞰摘要
L1（概览层） ~2000 tokens
    ↓ 提供结构化目录 + 每节预览
L2（切片层） ~chunks/*.md
    ↓ 具体的原始文本切片
```

**生成顺序**：先生成 L1，再从 L1 派生 L0。整个过程是**纯规则驱动**的，不调用 LLM。

#### L1 概览层生成

L1 是从所有 L2 切片文本中拼接生成的 Markdown 文档，分为两部分：

**① 全文摘要（`narrative`）**：
```python
full_body = "\n\n".join(chunk.text for chunk in chunks)  # 拼接所有切片的文本
narrative = truncate_to_tokens(full_body, max(1000, l1_max_tokens // 2))  # 截断到 ~1000-1500 tokens
```
将所有切片的原始文本直接拼接，然后按 token 上限粗暴截断。Token 估算采用简单规则：1 token ≈ 4 字符。

**② 结构化目录（`Structure`）**：
遍历所有 `level > 0` 的切片（即被解析器标记为标题的块），为每个标题生成一行目录条目：
```markdown
- **章节标题** (L2: `chunks/0034.md`): 下一个切片的预览内容...
```
每个章节的预览内容取自该标题切片的下一个切片文本，截断到 200 tokens。

如果文档没有标题级别的切片（`level` 全为 0），则回退为纯切片列表（最多列出前 50 个切片）。

最终整个 L1 Markdown 被 `clean_text` 清洗（合并多余空白/换行）后，截断到 **~2000 tokens**。

**L1 样本输出**：
```markdown
# Document overview

[全文拼接摘要，上限 ~1500 tokens]

## Structure

- **第一章标题** (L2: `chunks/0000.md`): 第一章内容预览...
- **第二章标题** (L2: `chunks/0001.md`): 第二章内容预览...
```

#### L0 摘要层生成

L0 **完全从已生成的 L1 文本中提取**，不依赖 LLM：

```python
def _abstract_from_overview(self, overview_md):
    sentences = extract_key_sentences(text, max_sentences=3)  # 最多取 3 句
    candidate = " ".join(sentences)
    return truncate_to_tokens(candidate, 100)  # 截断到 ~100 tokens
```

`extract_key_sentences` 的核心逻辑（`hierarchy/utils.py`）：
1. 按 `\n\n` 把 L1 文本拆成段落
2. 取每个段落的第一句话（按 `.` `!` `?` 分割）
3. 最多取 3 句，拼接后截断到 100 tokens

#### L2 切片层

L2 就是步骤 2 分块引擎产出的原始切片列表，不做额外处理，后续由 `HierarchyStorage` 将每个切片存为独立的 `chunks/NNNN.md` 文件。

#### 关键参数（可配置）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `l0_max_tokens` | 100 | L0 摘要最大 token 数 |
| `l1_max_tokens` | 2000 | L1 概览整体 token 上限 |
| `l1_section_preview_tokens` | 200 | L1 目录中每个章节的预览长度 |

#### 涉及的核心文件

| 文件 | 作用 |
|------|------|
| `hierarchy/document_hierarchy_builder.py` | 层级构建主逻辑（`build_hierarchy` 入口） |
| `hierarchy/utils.py` | 工具函数（`extract_key_sentences`、`truncate_to_tokens`、`clean_text`） |
| `hierarchy/models.py` | 数据结构（`HierarchyResult`：l0/l1/l2 三个字段） |
| `hierarchy/hierarchy_storage.py` | 将 L0/L1/L2 写入本地文件系统 |
| `processors/document_processor.py` | 流水线编排，步骤 3 调用 `build_hierarchy(chunks)` |

### 步骤 4：向量化（Embedding）

**技术**：调用 OpenAI Embedding API（默认 `text-embedding-3-small`，维度 1536）。

**解决的问题**：把文本转成数字向量。语义相近的文本，向量也相近。这是"语义搜索"的基础。

**特性**：
- 支持批量 Embedding（默认每批 8 条）
- 内置 LRU 缓存（SHA256 去重），相同文本不重复调用 API
- 没有 API Key 时回退到确定性 Mock（方便开发测试）
- 支持自定义 Base URL（兼容各种代理和兼容 API）

### 步骤 5：向量存储

**技术**：Milvus（开源向量数据库）。

**解决的问题**：几百万个向量，怎么快速找到与查询向量最相似的那几个？关系型数据库做不到，需要专用的向量数据库。

**索引类型**：`IVF_FLAT`（倒排文件 + 精确搜索），度量方式 COSINE（余弦相似度）。

**存储内容**：
- `chunk_id`：切片唯一 ID
- `file_id`：所属文件
- `text`：原文（最长 65535 字符）
- `embedding`：向量（1536 维）
- `page`：页码
- `level`：层级

**分层存储** (`MilvusLayerStore`)：L0 和 L1 的向量也存入 Milvus 独立的集合（`openrag_layers`），供上下文检索使用。

### 步骤 5a（可选）：Elasticsearch 全文索引

将每个切片的正文导入 Elasticsearch，为后续检索提供 BM25 关键词匹配能力。

### 步骤 6：层级文件存储

将 L0 摘要、L1 概览、L2 切片保存到 **MinIO**（或本地文件系统）。

### 步骤 7：更新元数据

更新文件状态为 `completed`，记录总切片数和总 Token 数。

---

## 6. 检索功能

### 6.1 检索架构

**技术**：自研 `RetrievalService`，整合 Milvus + Elasticsearch + CrossEncoder 重排序 + LLM 导航。

**检索模式**：

#### 6.1.1 平面检索（Flat）

```
查询文本 → Embedding → Milvus ANN 搜索 → 权限过滤 → 返回 Top-K
                                    ↘ 可选：ES BM25 融合
```

最简单的模式，直接把查询向量化后在向量库里找最近邻。

**Dense + Sparse 独立召回融合**（`vector_similarity_weight`）：
- Dense 与 ES BM25 在同一有限授权文件范围内独立召回，按 `chunk_id` 求并集。
- 使用 Weighted RRF：`w/(60+dense_rank) + (1-w)/(60+sparse_rank)`；`w=1` 为纯 Dense，`w=0` 为纯 Sparse。
- `fused_score` 只用于当前查询内排序，不是概率、余弦相似度，也不可跨查询比较；融合后统一经过 DB 权威校验。

#### 6.1.2 上下文检索（Contextual，默认推荐）

```
查询文本 → Embedding
    │
    ├─→ L0 向量搜索 → 选出 Top-N 候选文件
    │
    ├─→ L1 向量搜索（仅在候选文件中）→ 辅助打分
    │
    └─→ L2 向量搜索（仅在候选文件中）→ 加权融合
         │
         公式：final_score = 0.2×L0分 + 0.3×L1分 + 0.5×L2分
```

**优势**：避免在所有文件中盲目搜索。先用 L0 摘要找到"这篇文档大概在讲什么"，再用 L1 概览确认"文档结构是否匹配"，最后在候选文件里精确搜索 L2 切片。速度更快，结果更准。

#### 6.1.3 检索策略（意图路由）

| 策略 | 说明 | 使用场景 |
|------|------|----------|
| `auto` | 自动分析查询意图，选择最佳策略 | 默认 |
| `light` | 轻量召回，L0=22, L1=15, 倍率=2 | 简单查询 |
| `deep` | 深度检索，更大候选窗口 | 复杂/长查询 |
| `precise` | 精确检索，加宽平面搜索（不缩小候选） | 已知找特定内容 |
| `flat` | 纯平面检索 | 不需要层级导航 |

#### 6.1.4 L1 LLM 导航（可选高级功能）

```
L1 概览文本 + 文件名 + 切片数 → 发给 LLM
    ↓
LLM 分析查询意图，返回每个文件应检索哪些切片下标
    ↓
过滤向量命中结果，只保留 LLM 判定相关的切片
```

当启用 `use_l1_llm_navigation=True` 时，系统会调用 OpenAI 来辅助判断查询与文件结构的匹配度，从 L1 概览中推断最相关的切片范围，进一步提升精度。

### 6.2 重排序（Reranking）

**技术**：CrossEncoder 模型重排序。

**解决的问题**：向量相似度 ≠ 真正的语义相关性。向量搜索返回 Top-30，再用 CrossEncoder 模型逐个打分（query + 切片文本），选出真正的 Top-10。

**层级增强**：
- `/search/hierarchical` 端点会给非顶层标题的内容增加 0.15 的层级加分，提升标题/章节级别的结果。

### 6.3 权限过滤

每次检索都会自动过滤：
1. 管理员：可以搜所有文件
2. 指定了 workspace_id：仅搜该工作空间的文件
3. 未指定 workspace：仅搜用户有 read 权限的工作空间中的文件

### 6.4 检索 API

| 端点 | 说明 |
|------|------|
| `POST /search` 或 `POST /search/semantic` | 语义搜索（支持重排序） |
| `POST /search/hierarchical` | 层级搜索（标题感知，带层级加分） |
| `GET /search/chunks/{chunk_id}` | 获取切片详情 + 前后相邻切片（上下文） |

---

## 7. 权限系统

OpenRag 的内容访问权限以 **工作空间 RBAC** 为唯一用户授权边界；分享链接是独立的外部访问能力：

### 7.1 权限层级概览

```
系统管理员（is_admin）
    │ → 所有工作空间的所有权限
    ▼
工作空间级别
    │ 直接成员（WorkspaceMember: read / write）
    │ 角色权限（Role → RoleWorkspacePermission: read / write）
    │ → 文件列表、读取、搜索和写操作均继承工作空间权限
    ▼
分享链接
    │ Token + 可选密码 + 过期时间 + 最大访问次数
```

### 7.2 工作空间权限

**直接成员**：User → WorkspaceMember → Workspace
- `read`：可查看和搜索文档
- `write`：可上传、修改、删除文档

**角色权限**：User → UserRole → Role → RoleWorkspacePermission → Workspace
- 一个角色可以在多个工作空间有不同权限
- 一个用户可以有多个角色
- 直接成员与角色权限合并时取最高权限（write > read）；系统管理员独立拥有全部工作空间权限

### 7.3 文件访问边界

- 工作空间 `read`：可列出、读取、预览、下载和搜索该工作空间内的文件。
- 工作空间 `write`：包含 `read`，并可上传、移动、删除和重新处理文件。
- `File.owner_id` 只记录创建/归属审计信息，不产生额外读取或写入权限。
- Team 关系不参与文件授权；同一工作空间内不提供用户级或团队级文件 ACL。

### 7.4 分享链接

**解决的问题**：临时分享给外部人员（没有系统账号的人）。

**功能**：
- 生成唯一 Token（`secrets.token_urlsafe(32)`）
- 可选密码保护（bcrypt 哈希）
- 可选过期时间
- 可选最大访问次数（用完即失效）
- 访问计数器

**验证流程**：
```
Token 存在？→ 已过期？→ 超出访问次数？→ 密码正确？→ ✅ 返回文件
```

### 7.5 服务令牌（Service Token）

**解决的问题**：程序化访问 API（CI/CD 流水线、自动化脚本），不需要每次都人肉登录。

**技术**：通过 `X-OpenRag-Token` HTTP Header 传递令牌，后端解析为 `ServiceTokenContext`（包含模拟用户身份和工作空间权限）。

---

## 8. 任务调度与后台 Worker

### 8.1 任务模型 (`Task`)

每个文档处理操作都对应一个任务记录，包含：
- `task_id`：唯一任务标识（UUID）
- `task_type`：`process_document` / `parse_document` / `build_hierarchy` / `embed_document` / `delete_file` / `delete_path_prefix`
- `queue`：`fast` / `normal` / `slow`
- `priority`：优先级分数（0-10）
- `status`：`pending` → `assigned` → `started` → `success` / `failure`
- `retry_count` / `max_retries`：重试机制（默认最多 3 次）
- `heartbeat_at`：心跳时间（超时检测）
- `worker_id`：执行此任务的 Worker 标识

### 8.2 任务调度器（Task Broker）

**技术**：**纯嵌入式 Broker**（不依赖 Celery / Redis / RabbitMQ），基于 PostgreSQL 做任务队列。

**核心机制**：

1. **SELECT ... FOR UPDATE SKIP LOCKED**：多个 Worker 同时抢任务时，PostgreSQL 的行级锁保证每个任务只被一个 Worker 拿到。`SKIP LOCKED` 确保不会互相阻塞。

2. **动态权重调度**：
   - 每个工作空间有一个"动态权重"（初始 = `max_concurrent_tasks`）
   - 每次分配任务后，该工作空间权重 × 0.8（衰减）
   - 所有工作空间权重都接近 0 时，集体重置
   - 保证公平：不会出现某个工作空间霸占所有 Worker

3. **并发控制**：
   - 统计活跃 Worker 数量
   - 全局任务上限 = 活跃 Worker 数
   - 每个工作空间还有各自的 `max_concurrent_tasks` 上限

4. **超时恢复**：
   - 后台定时器每 60 秒扫描
   - 超过 10 分钟没心跳的 ASSIGNED/STARTED 任务 → 重置为 PENDING

### 8.3 Worker 进程 (`run_worker.py`)

**架构**：多进程模型，每个 Worker 是一个独立的 Python 进程。

**工作流程**：
```
循环：
    1. HTTP GET /broker/tasks/claim?worker_id=xxx&limit=1
    2. 拿到任务 → 开始处理
    3. 定时发送心跳 HTTP POST /broker/tasks/heartbeat
    4. 调用 DocumentProcessor.process_document()
    5. 更新任务状态为 success 或 failure
    6. 如果失败 → 判断是否需要重试
    7. 继续下一个任务
```

- Worker 崩溃会自动重启（进程管理）
- HTTP 拉取模式（pull-based），Worker 闲置时不会占用数据库连接

---

## 9. 外部数据源连接器

OpenRag 不仅支持手动上传文件，还内置了 **20+ 种外部数据源连接器**，可以从第三方平台自动同步文档。这些连接器位于 `openrag/common/data_source/` 下。

### 支持的数据源

| 类别 | 连接器 |
|------|--------|
| **代码托管** | GitHub, GitLab, Bitbucket |
| **协作文档** | Google Drive, Notion, Confluence, SharePoint |
| **项目管理** | Jira, Asana |
| **沟通工具** | Slack, Discord, Microsoft Teams, Gmail |
| **文件存储** | Dropbox, Box, WebDAV, Seafile |
| **数据库** | RDBMS（关系型数据库） |
| **其他** | RSS, Airtable, Zendesk, Moodle, IMAP（邮件）, 钉钉智能表格 |

### 连接器通用模式

所有连接器遵循统一的接口 (`interfaces.py`)：
```
连接 → 认证（OAuth / API Token） → 列出可访问资源 → 下载/同步内容 → 格式转换 → 进入处理流水线
```

这个模块的设计让 OpenRag 不只是"文件网盘 + 搜索"，而是一个 **企业知识聚合中心**——把散落在各 SaaS 平台的知识统一索引、统一检索。

---

## 10. 技术栈总结

### 后端核心

| 技术 | 用途 | 为什么选它 |
|------|------|-----------|
| **Python 3.12+** | 主语言 | AI/ML 生态最完善 |
| **FastAPI** | Web 框架 | 异步支持、自动生成 OpenAPI 文档、类型安全 |
| **Pydantic v2** | 数据验证/配置管理 | FastAPI 标配，类型安全 |
| **SQLAlchemy 2.0** | ORM | Python 最成熟的 ORM |
| **PostgreSQL** | 元数据存储 | 支持行级锁（SKIP LOCKED）、JSON 字段 |

### AI / 检索

| 技术 | 用途 |
|------|------|
| **OpenAI Embedding API** | 文本向量化（text-embedding-3-small，1536 维） |
| **Milvus** | 向量数据库，ANN 近似最近邻搜索 |
| **Elasticsearch** | 全文检索引擎，BM25 关键词匹配 |
| **CrossEncoder** | 重排序模型，提升检索精度 |
| **RAGFlow deepdoc** | 文档解析引擎（PDF/Word/Excel/PPT 等） |
| **Docling / MinerU / PaddleOCR** | PDF 深度解析（布局识别、表格识别、OCR） |

### 存储

| 技术 | 用途 |
|------|------|
| **MinIO** | 对象存储（兼容 S3），存原始文件和处理产物 |
| **bcrypt** | 密码哈希 |
| **PyJWT (jose)** | JWT 令牌签发与验证 |

### 安全

| 机制 | 说明 |
|------|------|
| JWT + HS256 | 无状态 API 认证 |
| bcrypt (12 rounds) | 密码存储 |
| 多租户隔离 | 按 Workspace 数据隔离 |
| RBAC + ACL | 角色权限 + 文件级权限 + 继承 |
| 路径遍历防护 | 文件路径校验 |
| CORS | 限制允许的前端域名 |
| SQL 错误隐藏 | 生产模式不暴露内部 SQL 错误 |

### 架构特性

- **异步任务处理**：上传后立即返回，Worker 后台处理
- **嵌入式任务队列**：不依赖 Redis/RabbitMQ，纯 PostgreSQL 实现
- **多进程 Worker**：自动重启，HTTP 拉取模式
- **动态权重调度**：公平分配任务资源
- **超时恢复**：心跳检测 + 自动重置

---

## 附录：项目目录结构（核心源码）

```
openrag/
├── run_api.py                  # API 服务入口（FastAPI, 端口 8000）
├── run_worker.py               # Worker 进程入口
├── setup.py                    # 包安装配置
│
├── src/openrag/
│   ├── config.py               # 全局配置（环境变量 + YAML）
│   ├── database.py             # 数据库连接管理（PostgreSQL）
│   ├── security.py             # JWT + bcrypt 安全工具
│   ├── scheduler.py            # 后台定时器（超时任务恢复）
│   │
│   ├── api/                    # API 路由层
│   │   ├── main.py             # FastAPI 应用入口 + 路由注册
│   │   ├── deps.py             # 依赖注入（认证、权限检查）
│   │   ├── auth.py             # JWT 认证逻辑
│   │   ├── users_api.py        # 用户注册/登录/管理
│   │   ├── workspaces_api.py   # 工作空间 CRUD + 成员管理
│   │   ├── teams_api.py        # 团队 CRUD + 成员管理
│   │   ├── files_api.py        # 文件上传/列表/预览/删除/移动
│   │   ├── search_api.py       # 语义搜索 + 层级搜索
│   │   ├── share_api.py        # 分享链接创建/验证/访问
│   │   ├── tasks_api.py        # 任务状态查询/管理
│   │   ├── permissions_api.py  # 用户工作空间权限详情查询
│   │   ├── roles_api.py        # 角色 + 工作空间权限
│   │   ├── broker_api.py       # Worker 认领任务/心跳
│   │   ├── service_api.py      # M2M 服务令牌 API
│   │   └── service_tokens_admin.py  # 服务令牌管理
│   │
│   ├── models/                 # 数据模型（SQLAlchemy ORM）
│   │   ├── user.py, workspace.py, team.py
│   │   ├── file.py, document_chunk.py
│   │   ├── task.py
│   │   ├── role.py, share.py
│   │   ├── service_token.py, audit.py
│   │   └── base.py             # 基础模型（Base, TimestampMixin）
│   │
│   ├── services/               # 业务逻辑层
│   │   ├── user_manager.py     # 用户管理
│   │   ├── workspace_service.py # 工作空间管理
│   │   ├── team_manager.py     # 团队管理
│   │   ├── share_manager.py    # 分享链接管理
│   │   ├── task_service.py     # 任务管理
│   │   ├── file_ingest.py      # 文件摄入（上传/替换）
│   │   ├── file_deletion.py    # 文件删除（级联清理）
│   │   ├── file_preview.py     # 文件预览生成
│   │   ├── quota_service.py    # 配额管理
│   │   ├── service_token_service.py # 服务令牌认证
│   │   └── audit_logger.py     # 审计日志
│   │
│   ├── parsers/                # 文档解析器
│   │   ├── base.py             # 解析器抽象基类
│   │   ├── factory.py          # 懒加载解析器工厂
│   │   ├── parser_registry.py  # 解析器注册表（按扩展名+类型路由）
│   │   ├── ragflow_parser.py   # RAGFlow 解析器入口
│   │   ├── adapters/           # 格式适配器（pdf/docx/xlsx/pptx/html/md/txt/json/epub）
│   │   └── ragflow/            # RAGFlow 引擎封装（parser + vision 视觉识别）
│   │
│   ├── chunking/               # 分块引擎
│   │   ├── chunk_engine.py     # 多策略分块（semantic/paragraph/fixed_size）
│   │   ├── chunk_models.py     # Chunk 数据结构
│   │   ├── chunk_params.py     # 分块参数（环境变量解析）
│   │   └── ragflow_core/       # RAGFlow 语义分块实现
│   │
│   ├── embedding/              # 向量化
│   │   └── embedding_engine.py # OpenAI Embedding + 缓存 + Mock
│   │
│   ├── hierarchy/              # 文档层级（L0/L1/L2）
│   │   ├── document_hierarchy_builder.py  # 层级构建器
│   │   ├── hierarchy_storage.py           # 层级文件存储（本地）
│   │   └── models.py                      # HierarchyResult 数据结构
│   │
│   ├── processors/             # 文档处理编排
│   │   └── document_processor.py  # 7 步流水线编排器
│   │
│   ├── retrieval/              # 检索服务
│   │   ├── retrieval_service.py # 核心检索（平面/上下文/融合）
│   │   ├── reranker.py          # CrossEncoder 重排序
│   │   ├── query_intent.py      # 查询意图分析（auto/light/deep/precise/flat）
│   │   ├── l1_llm_navigator.py  # L1 LLM 导航
│   │   └── filters.py           # 检索过滤器
│   │
│   ├── vectorstore/            # 向量存储
│   │   ├── milvus_store.py      # Milvus 切片向量存储
│   │   └── milvus_layer_store.py # Milvus L0/L1 分层向量存储
│   │
│   ├── storage/                # 对象存储
│   │   └── minio_storage.py    # MinIO 存储（文件 + 层级 + 切片）
│   │
│   ├── broker/                 # 任务调度
│   │   └── task_broker.py      # 动态权重调度 + SKIP LOCKED
│   │
│   └── worker/                 # Worker
│       └── task_worker.py      # 任务消费 + 心跳 + 重试
│
├── common/                     # 公共工具库
│   ├── data_source/            # 20+ 外部数据源连接器
│   ├── doc_store/              # 文档存储抽象层（ES, Infinity, OceanBase）
│   └── *.py                    # 工具函数（加密、HTTP、日志、时间...）
│
├── rag/                        # RAG 增强模块
│   ├── flow/                   # RAG 流水线（解析/分块/提取/分词）
│   ├── llm/                    # LLM 模型适配（Chat/Embedding/CV/OCR/Rerank/TTS）
│   ├── graphrag/               # GraphRAG（知识图谱检索增强）
│   ├── nlp/                    # NLP 工具（分词/搜索/同义词/加权）
│   ├── prompts/                # Prompt 模板
│   ├── utils/                  # 存储连接器（S3/MinIO/OSS/Azure/GCS...）
│   └── app/                    # 应用层（特定领域 RAG 配置）
│
└── tests/                      # 测试（120+ 测试文件）
    ├── test_*.py               # 单元测试
    └── parity/                 # RAGFlow 输出一致性测试
```

---

> 本文档基于 `openrag/` 目录源码分析生成，版本 dev 分支，生成日期 2026-05-13。
