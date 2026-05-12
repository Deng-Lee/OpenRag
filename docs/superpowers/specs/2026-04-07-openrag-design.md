---
name: OpenRag 系统设计
description: 融合 OpenViking 和 RAGFlow 优势的文档管理与检索系统
type: system-design
---

# OpenRag 系统设计文档

## 1. 项目概述

### 1.1 项目目标

OpenRag 是一个融合 OpenViking 和 RAGFlow 优势的文档管理与检索系统，旨在提供：

1. **文件系统范式的访问方式** - 对外暴露文件路径形式的接口，而非数据集方式
2. **完善的权限控制** - 继承 OpenViking 的多租户和目录权限设计
3. **智能文档切片** - 使用 RAGFlow 的切片机制，保存完整位置信息
4. **分层内容组织** - 结合 OpenViking 的 L0/L1/L2 分层和 RAGFlow 的 chunk 粒度
5. **灵活的向量检索** - 可插拔的向量数据库，支持多种检索策略

### 1.2 核心特性

- ✅ 文件路径访问（非数据集模式）
- ✅ 目录级和文件级权限控制
- ✅ 公有/私有/共享文件支持
- ✅ RAGFlow 文档解析和切片
- ✅ 完整的位置信息保存（页码、偏移量、PDF坐标、标题层级）
- ✅ OpenViking L0/L1/L2 分层结构
- ✅ 可插拔存储后端（AGFS/S3/Local）
- ✅ 可插拔向量数据库（Milvus/Qdrant/Elasticsearch）
- ✅ RESTful API + Web UI + SDK
- ✅ Docker 容器化部署

## 2. 系统架构

### 2.1 整体架构（深度集成 OpenViking）

```
┌─────────────────────────────────────────────────────────────┐
│                      OpenRag 系统架构                        │
│              (基于 OpenViking 的企业文档管理系统)            │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  OpenRag 企业功能层                                          │
│  ├─ RESTful API (FastAPI)                                   │
│  ├─ Web UI (React)                                          │
│  ├─ Python/JavaScript SDK                                   │
│  ├─ PermissionManager (企业级权限控制)                      │
│  ├─ ShareManager (共享链接管理)                             │
│  ├─ UserManager (用户/团队管理)                             │
│  └─ AuditLogger (审计日志)                                  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │         RAGFlow 文档处理增强层                        │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  RAGFlow Parser (deepdoc)                      │  │  │
│  │  │  - OCR + 布局识别 + 表格识别                   │  │  │
│  │  │  - 多格式支持 (PDF/DOCX/HTML/MD)              │  │  │
│  │  └────────────────────────────────────────────────┘  │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  增强切片引擎                                   │  │  │
│  │  │  - 多种切片策略 (段落/语义/固定长度)          │  │  │
│  │  │  - 完整位置信息 (页码/偏移/坐标/层级)         │  │  │
│  │  └────────────────────────────────────────────────┘  │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  RAGFlow Embedding                             │  │  │
│  │  │  - 批量向量化                                   │  │  │
│  │  │  - 多模型支持                                   │  │  │
│  │  └────────────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────┘  │
│                          ↓                                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              OpenViking 核心引擎                      │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  VikingFS (虚拟文件系统)                       │  │  │
│  │  │  - URI 抽象 (viking://)                        │  │  │
│  │  │  - 存储适配器 (AGFS/S3/Local)                 │  │  │
│  │  └────────────────────────────────────────────────┘  │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  Hierarchical Retriever (分层检索)            │  │  │
│  │  │  - 意图分析                                     │  │  │
│  │  │  - 目录递归检索                                 │  │  │
│  │  │  - L0/L1/L2 层级检索                           │  │  │
│  │  └────────────────────────────────────────────────┘  │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  TreeBuilder (树构建器)                        │  │  │
│  │  │  - 目录树构建                                   │  │  │
│  │  │  - L0/L1/L2 生成                               │  │  │
│  │  └────────────────────────────────────────────────┘  │  │
│  │  ┌────────────────────────────────────────────────┐  │  │
│  │  │  向量数据库抽象层                               │  │  │
│  │  │  - 可插拔适配器 (Milvus/Qdrant/ES)            │  │  │
│  │  └────────────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────┘  │
│                          ↓                                   │
│  数据存储层                                                  │
│  ├─ MySQL (OpenRag 权限 + OpenViking 元数据)               │
│  ├─ AGFS/S3 (文件内容、切片内容)                            │
│  └─ 向量数据库 (向量索引)                                   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 架构说明

**分层职责：**

1. **OpenRag 企业功能层** - 新增的企业级功能
   - 用户/团队管理
   - 企业级权限控制（ACL）
   - 共享链接（密码、过期、访问控制）
   - Web UI 和 API
   - 审计日志

2. **RAGFlow 文档处理增强层** - 增强的文档处理能力
   - 使用 RAGFlow deepdoc 进行深度文档解析
   - 多种切片策略（比 OpenViking 更强大）
   - 完整的位置信息提取

3. **OpenViking 核心引擎** - 复用 OpenViking 的核心能力
   - VikingFS 虚拟文件系统
   - 分层检索（L0/L1/L2）
   - 目录递归检索
   - TreeBuilder
   - 向量数据库抽象

**集成方式：**

```python
# OpenRag 使用 OpenViking 作为底层引擎
from openviking import OpenViking, VikingFS, TreeBuilder

class OpenRag:
    def __init__(self, config):
        # 初始化 OpenViking
        self.viking = OpenViking(config['openviking'])
        
        # 初始化 RAGFlow 组件
        self.ragflow_parser = RAGFlowParser()
        self.chunk_engine = EnhancedChunkEngine()
        
        # 初始化 OpenRag 企业功能
        self.permission_manager = PermissionManager(config['mysql'])
        self.share_manager = ShareManager(config['mysql'])
        self.user_manager = UserManager(config['mysql'])
```

### 2.2 技术栈

**OpenRag 新增组件：**
- Python 3.10+
- FastAPI (Web 框架)
- MySQL 8.0 (权限、用户、审计日志)
- SQLAlchemy (ORM)
- Celery + Redis (异步任务队列)
- React 18 + TypeScript (前端)
- Ant Design (UI 组件)

**OpenViking 核心组件（复用）：**
- VikingFS (虚拟文件系统)
- AGFS (存储后端)
- Hierarchical Retriever (分层检索)
- TreeBuilder (树构建器)
- 向量数据库抽象层

**RAGFlow 组件（集成）：**
- deepdoc (文档解析)
  - PDF Parser (OCR + 布局识别 + 表格识别)
  - Markdown/HTML/DOCX Parser
- rag.nlp (切片和 tokenizer)
- Embedding 引擎

**部署：**
- Docker & Docker Compose
- Nginx (反向代理)

## 3. 数据模型设计

### 3.1 MySQL 数据库表

#### 用户与团队

```sql
-- 用户表
CREATE TABLE users (
    id VARCHAR(64) PRIMARY KEY,
    username VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- 团队表
CREATE TABLE teams (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    owner_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- 团队成员表
CREATE TABLE team_members (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    team_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    role ENUM('owner', 'admin', 'member') DEFAULT 'member',
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE KEY uk_team_user (team_id, user_id)
);
```

#### 文件与权限

```sql
-- 文件元数据表
CREATE TABLE files (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    file_path VARCHAR(1024) UNIQUE NOT NULL,
    filename VARCHAR(255) NOT NULL,
    file_type VARCHAR(50),
    file_size BIGINT,
    owner_id VARCHAR(64) NOT NULL,
    parent_path VARCHAR(1024),
    visibility ENUM('private', 'public', 'shared') DEFAULT 'private',
    is_directory BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP NULL,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);

-- 文件权限表（ACL）
CREATE TABLE file_permissions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    file_id BIGINT NOT NULL,
    grantee_type ENUM('user', 'team', 'public') NOT NULL,
    grantee_id VARCHAR(64),
    can_read BOOLEAN DEFAULT FALSE,
    can_write BOOLEAN DEFAULT FALSE,
    can_delete BOOLEAN DEFAULT FALSE,
    can_share BOOLEAN DEFAULT FALSE,
    can_manage BOOLEAN DEFAULT FALSE,
    granted_by VARCHAR(64) NOT NULL,
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NULL,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    UNIQUE KEY uk_file_grantee (file_id, grantee_type, grantee_id)
);

-- 共享链接表
CREATE TABLE share_links (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    share_id VARCHAR(64) UNIQUE NOT NULL,
    file_id BIGINT NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    password_hash VARCHAR(255),
    can_read BOOLEAN DEFAULT TRUE,
    can_write BOOLEAN DEFAULT FALSE,
    can_delete BOOLEAN DEFAULT FALSE,
    is_public BOOLEAN DEFAULT FALSE,
    access_count INT DEFAULT 0,
    max_access INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NULL,
    revoked_at TIMESTAMP NULL,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
);
```

#### 文档切片

```sql
-- 文档切片元数据表
CREATE TABLE document_chunks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    file_id BIGINT NOT NULL,
    chunk_index INT NOT NULL,
    
    -- 存储路径（VikingFS URI）
    content_path VARCHAR(512) NOT NULL,
    content_hash VARCHAR(64),
    content_length INT,
    token_count INT,
    
    -- 位置信息
    page_number INT,
    char_offset_start INT,
    char_offset_end INT,
    bbox JSON,
    
    -- 层级信息
    heading_level INT,
    heading_text VARCHAR(500),
    parent_chunk_id BIGINT,
    
    -- 分层标识
    layer_type ENUM('L0', 'L1', 'L2') DEFAULT 'L2',
    
    -- 向量化信息
    vector_id VARCHAR(128),
    embedding_model VARCHAR(100),
    vector_status ENUM('pending', 'completed', 'failed') DEFAULT 'pending',
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_chunk_id) REFERENCES document_chunks(id) ON DELETE SET NULL
);
```

### 3.2 VikingFS 目录结构

```
openrag://
├── public/                          # 公共空间
│   └── templates/
├── shared/                          # 共享空间
│   └── {share_id}/
├── users/                           # 用户空间
│   └── {user_id}/
│       ├── private/                 # 私有文件
│       └── work/                    # 工作文件
├── files/                           # 文件存储
│   └── {file_id}/
│       ├── original.pdf             # 原始文件
│       ├── .abstract.md             # L0 摘要
│       ├── .overview.md             # L1 概览
│       └── .metadata.json           # 元数据
└── chunks/                          # 切片存储
    └── {file_id}/
        ├── {chunk_id_1}.txt
        ├── {chunk_id_2}.txt
        └── ...
```

### 3.3 向量数据库 Schema

```python
# Collection: openrag_chunks
{
    "id": "chunk_{chunk_id}",           # 主键
    "vector": [0.1, 0.2, ...],          # 1024维向量
    "chunk_id": 12345,                  # 关联到 MySQL
    "file_id": 123,                     # 文件ID
    "layer_type": "L2"                  # 层级类型
}
```

## 4. 核心功能设计

### 4.1 文件管理（基于 OpenViking）

**目录结构（使用 OpenViking 的 VikingFS）：**
```
viking://
├── public/                          # 公共空间
│   └── templates/
├── shared/                          # 共享空间
│   └── {share_id}/
├── users/                           # 用户空间
│   └── {user_id}/
│       ├── private/                 # 私有文件
│       └── work/                    # 工作文件
└── teams/                           # 团队空间
    └── {team_id}/
```

**权限模型（OpenRag 新增）：**
- 权限类型：read, write, delete, share, manage
- 权限级别：文件级 > 目录级 > 团队级 > 空间级
- 权限继承：子目录/文件继承父目录权限，可覆盖
- **与 OpenViking 集成：** 在 OpenViking 的文件操作之上添加权限检查层

### 4.2 文档处理流程（RAGFlow 增强 + OpenViking 存储）

```
1. 文件上传（OpenRag API）
   ↓
2. 权限检查（OpenRag PermissionManager）
   ↓
3. RAGFlow Parser 深度解析
   - PDF: OCR + 布局识别 + 表格识别
   - Markdown: 按标题分割
   - DOCX/HTML: 结构化提取
   ↓
4. 增强切片引擎（RAGFlow 切片策略）
   - 策略：paragraph / semantic / fixed_length
   - 提取完整位置信息（页码、偏移、坐标、层级）
   - 比 OpenViking 默认切片更强大
   ↓
5. 调用 OpenViking TreeBuilder
   - 构建目录树结构
   - 生成 L0/L1/L2 分层
   - 存储到 VikingFS
   ↓
6. Embedding 向量化（异步）
   - 使用 RAGFlow embedding 方法
   - 批量处理（batch_size=100）
   ↓
7. 存储到 OpenViking
   - VikingFS: 原始文件、L0/L1/L2、切片内容
   - 向量库: 向量 + 元数据
   ↓
8. 更新 OpenRag 权限表
   - MySQL: 文件元数据、权限配置
```

**关键集成点：**

```python
class DocumentProcessor:
    def __init__(self, viking: OpenViking, config):
        self.viking = viking
        self.ragflow_parser = RAGFlowParser()
        self.chunk_engine = EnhancedChunkEngine()
        self.permission_manager = PermissionManager(config)
    
    async def process_document(
        self,
        file_path: str,
        user_id: str,
        visibility: str = 'private'
    ):
        # 1. 权限检查（OpenRag）
        if not self.permission_manager.can_upload(user_id):
            raise PermissionError()
        
        # 2. RAGFlow 深度解析
        parse_result = self.ragflow_parser.parse(file_path)
        
        # 3. 增强切片（保留完整位置信息）
        chunks = self.chunk_engine.chunk(
            parse_result,
            strategy='semantic',
            preserve_location=True  # 关键：保留位置信息
        )
        
        # 4. 转换为 OpenViking 格式并存储
        viking_uri = f"viking://users/{user_id}/work/{filename}"
        
        # 使用 OpenViking 的 TreeBuilder
        tree_result = self.viking.tree_builder.build_from_chunks(
            chunks=chunks,
            target_uri=viking_uri,
            generate_l0_l1=True  # 使用 OpenViking 的 L0/L1 生成
        )
        
        # 5. 向量化并存储（使用 OpenViking 的向量库）
        embeddings = await self.embed_chunks(chunks)
        self.viking.vector_db.insert(
            collection='openrag_chunks',
            vectors=embeddings,
            metadata=[{
                'chunk_id': c.id,
                'file_id': file_id,
                'uri': viking_uri,
                # 保留 RAGFlow 的位置信息
                'page_number': c.page_number,
                'bbox': c.bbox,
                'heading_level': c.heading_level
            } for c in chunks]
        )
        
        # 6. 保存权限配置（OpenRag）
        self.permission_manager.set_file_permission(
            file_path=viking_uri,
            owner_id=user_id,
            visibility=visibility
        )
```

### 4.3 检索策略（OpenViking 检索 + RAGFlow 增强）

**混合检索（充分利用 OpenViking 的核心能力）：**

1. **OpenViking 分层检索（核心）**
   ```python
   # 使用 OpenViking 的 Hierarchical Retriever
   results = self.viking.search(
       query="用户查询",
       uri="viking://users/{user_id}/work/",
       strategy='hierarchical'  # L0 → L1 → L2 递归检索
   )
   ```
   - 意图分析
   - 目录递归检索
   - L0/L1/L2 层级检索
   - 检索轨迹可视化

2. **向量检索（语义搜索）**
   ```python
   # 使用 OpenViking 的向量检索
   vector_results = self.viking.vector_db.search(
       query_vector=embedding,
       filters={
           'uri_prefix': f"viking://users/{user_id}/",
           # 利用 RAGFlow 的位置信息过滤
           'page_number': page,
           'heading_level': level
       }
   )
   ```

3. **Rerank 重排序（利用层级信息）**
   - 使用专门的 rerank 模型（如 bge-reranker-large）
   - **利用 RAGFlow 的位置信息优化：**
     - 如果查询匹配章节标题，提升该章节下所有 chunks 的分数
     - 利用 parent_chunk_id 进行上下文扩展
     - 利用 bbox 信息判断内容重要性（标题区域 vs 正文区域）
   - 基于文档结构的智能重排序

**集成示例：**

```python
class RetrievalService:
    def __init__(self, viking: OpenViking, config):
        self.viking = viking
        self.permission_manager = PermissionManager(config)
        self.reranker = Reranker()
    
    def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 10,
        use_hierarchical: bool = True
    ):
        # 1. 权限过滤：获取用户可访问的 URI 列表
        accessible_uris = self.permission_manager.get_accessible_uris(user_id)
        
        # 2. 使用 OpenViking 的分层检索
        if use_hierarchical:
            results = []
            for uri in accessible_uris:
                # OpenViking 的核心能力：分层递归检索
                uri_results = self.viking.search(
                    query=query,
                    uri=uri,
                    strategy='hierarchical',
                    top_k=top_k * 2
                )
                results.extend(uri_results)
        else:
            # 纯向量检索
            results = self.viking.search(
                query=query,
                uri_filter=accessible_uris,
                strategy='vector',
                top_k=top_k * 2
            )
        
        # 3. Rerank（利用 RAGFlow 的位置信息）
        reranked = self.reranker.rerank(
            query=query,
            results=results,
            use_location_info=True,  # 利用位置信息
            use_hierarchy=True       # 利用层级关系
        )
        
        return reranked[:top_k]
```

### 4.4 共享功能

**共享方式：**

1. **生成共享链接**
   - 唯一 share_id
   - 可设置密码
   - 可设置过期时间
   - 可设置访问次数限制
   - 可设置用户白名单

2. **直接授权**
   - 授权给特定用户
   - 授权给团队
   - 设置权限级别

## 5. API 设计

### 5.1 文件操作

```
POST   /api/v1/files/upload          # 上传文件
GET    /api/v1/files/{path}          # 获取文件
DELETE /api/v1/files/{path}          # 删除文件
PUT    /api/v1/files/{path}/move     # 移动文件
GET    /api/v1/files/{path}/list     # 列出目录
```

### 5.2 权限管理

```
GET    /api/v1/permissions/{path}              # 获取权限
PUT    /api/v1/permissions/{path}/grant        # 授予权限
DELETE /api/v1/permissions/{path}/revoke       # 撤销权限
```

### 5.3 共享管理

```
POST   /api/v1/share/create          # 创建共享链接
GET    /api/v1/share/{share_id}      # 访问共享文件
DELETE /api/v1/share/{share_id}      # 撤销共享
```

### 5.4 检索服务

```
POST   /api/v1/search/semantic       # 语义搜索
POST   /api/v1/search/hierarchical   # 分层搜索
GET    /api/v1/chunks/{chunk_id}     # 获取切片
```

## 6. 部署架构

### 6.1 Docker Compose 部署

```yaml
services:
  - mysql (元数据)
  - redis (任务队列)
  - milvus/qdrant (向量数据库)
  - minio (S3 兼容存储)
  - openrag-api (后端服务)
  - openrag-worker (Celery 异步任务)
  - openrag-web (前端服务)
  - nginx (反向代理)
```

### 6.2 配置文件

```yaml
# config.yaml
storage:
  type: s3  # agfs, s3, local
  config:
    endpoint: http://minio:9000
    bucket: openrag
    access_key: minioadmin
    secret_key: minioadmin

vector_db:
  type: milvus  # milvus, qdrant, elasticsearch
  config:
    host: milvus
    port: 19530

mysql:
  host: mysql
  port: 3306
  database: openrag
  user: root
  password: openrag_password

embedding:
  model: text-embedding-3-large
  provider: openai
  api_key: your_api_key
```

## 7. 关键技术点

### 7.1 深度集成 OpenViking

**集成策略：**

OpenRag 不是重新实现 OpenViking，而是在其之上构建企业功能层。

```python
# OpenRag 的核心类
class OpenRag:
    def __init__(self, config):
        # 1. 初始化 OpenViking（核心引擎）
        self.viking = OpenViking(
            storage_config=config['storage'],
            vector_db_config=config['vector_db']
        )
        
        # 2. 初始化 RAGFlow 组件（文档处理增强）
        self.ragflow_parser = RAGFlowParser()
        self.chunk_engine = EnhancedChunkEngine()
        self.embedding_engine = RAGFlowEmbedding(config['embedding'])
        
        # 3. 初始化 OpenRag 企业功能
        self.permission_manager = PermissionManager(config['mysql'])
        self.share_manager = ShareManager(config['mysql'])
        self.user_manager = UserManager(config['mysql'])
        self.audit_logger = AuditLogger(config['mysql'])
    
    # 文件上传：RAGFlow 解析 + OpenViking 存储 + OpenRag 权限
    def upload_file(self, file_path: str, user_id: str, **kwargs):
        # 权限检查（OpenRag）
        if not self.permission_manager.can_upload(user_id):
            raise PermissionError()
        
        # RAGFlow 深度解析
        parse_result = self.ragflow_parser.parse(file_path)
        
        # 增强切片（保留位置信息）
        chunks = self.chunk_engine.chunk(parse_result, **kwargs)
        
        # 使用 OpenViking 存储
        viking_uri = f"viking://users/{user_id}/work/{filename}"
        self.viking.add_resource(
            uri=viking_uri,
            chunks=chunks,
            generate_l0_l1=True
        )
        
        # 设置权限（OpenRag）
        self.permission_manager.set_permission(viking_uri, user_id, **kwargs)
        
        # 审计日志（OpenRag）
        self.audit_logger.log('upload', user_id, viking_uri)
    
    # 检索：OpenViking 检索 + OpenRag 权限过滤
    def search(self, query: str, user_id: str, **kwargs):
        # 获取可访问的 URI（OpenRag）
        accessible_uris = self.permission_manager.get_accessible_uris(user_id)
        
        # 使用 OpenViking 的分层检索
        results = self.viking.search(
            query=query,
            uri_filter=accessible_uris,
            strategy='hierarchical'
        )
        
        # 审计日志（OpenRag）
        self.audit_logger.log('search', user_id, query)
        
        return results
```

**复用 OpenViking 的核心能力：**
- ✅ VikingFS 虚拟文件系统
- ✅ 分层检索（L0/L1/L2）
- ✅ 目录递归检索
- ✅ TreeBuilder
- ✅ 向量数据库抽象
- ✅ 意图分析
- ✅ 检索轨迹可视化

**OpenRag 新增的企业功能：**
- ✅ 用户/团队管理
- ✅ 企业级权限控制
- ✅ 共享链接
- ✅ Web UI
- ✅ 审计日志
- ✅ RAGFlow 深度文档处理

### 7.2 RAGFlow 文档处理增强

**为什么需要 RAGFlow？**

OpenViking 的文档处理相对简单，主要面向 Agent 场景。OpenRag 需要更强大的文档处理能力：

| 功能 | OpenViking | OpenRag (RAGFlow) |
|------|-----------|-------------------|
| PDF 解析 | 基础文本提取 | OCR + 布局识别 + 表格识别 |
| 切片策略 | 简单标题分割 | 段落/语义/固定长度多种策略 |
| 位置信息 | 基本页码 | 页码 + 偏移 + PDF坐标 + 层级 |
| 表格处理 | 不支持 | 表格结构识别 + 内容提取 |
| 图片处理 | 不支持 | OCR + 图片描述 |

**集成方式：**

```python
class EnhancedChunkEngine:
    """增强的切片引擎（基于 RAGFlow）"""
    
    def __init__(self):
        from deepdoc.parser import PdfParser
        from rag.nlp import rag_tokenizer
        
        self.pdf_parser = PdfParser()
        self.tokenizer = rag_tokenizer
    
    def chunk(
        self,
        parse_result,
        strategy: str = 'semantic',
        chunk_size: int = 512,
        preserve_location: bool = True
    ):
        """
        使用 RAGFlow 的切片算法，但输出格式兼容 OpenViking
        """
        # RAGFlow 切片
        ragflow_chunks = self._ragflow_chunk(
            parse_result,
            strategy=strategy,
            chunk_size=chunk_size
        )
        
        # 转换为 OpenViking 兼容格式，同时保留位置信息
        viking_chunks = []
        for chunk in ragflow_chunks:
            viking_chunk = {
                'content': chunk.content,
                'metadata': {
                    # OpenViking 标准字段
                    'chunk_index': chunk.index,
                    'heading_level': chunk.heading_level,
                    'heading_text': chunk.heading_text,
                    
                    # RAGFlow 增强字段（保留）
                    'page_number': chunk.page_number,
                    'char_offset_start': chunk.char_offset_start,
                    'char_offset_end': chunk.char_offset_end,
                    'bbox': chunk.bbox,  # PDF 坐标
                    'block_type': chunk.block_type  # text/table/figure
                }
            }
            viking_chunks.append(viking_chunk)
        
        return viking_chunks
```

### 7.3 VikingFS 虚拟文件系统（直接使用 OpenViking）

**不需要重新实现，直接使用 OpenViking 的 VikingFS：**

```python
# 直接使用 OpenViking 的 VikingFS
from openviking import VikingFS

viking_fs = self.viking.get_viking_fs()

# 所有文件操作通过 OpenViking
viking_fs.read(uri)
viking_fs.write(uri, data)
viking_fs.list(uri)
viking_fs.move(src_uri, dst_uri)
```

### 7.4 分层内容组织（使用 OpenViking 的 TreeBuilder）

**L0/L1/L2 生成由 OpenViking 负责：**

```python
# 使用 OpenViking 的 TreeBuilder
tree_result = self.viking.tree_builder.build_from_chunks(
    chunks=chunks,  # RAGFlow 增强的 chunks
    target_uri=viking_uri,
    generate_l0_l1=True  # OpenViking 自动生成 L0/L1
)
```

**L0 层（摘要）：**
- 由 OpenViking TreeBuilder 生成
- 规则提取 + 可选 LLM 总结
- **用途：**
  - 快速预览和 token 优化
  - 文件浏览器展示
  - Rerank 时提供文档级上下文

**L1 层（概览）：**
- 由 OpenViking TreeBuilder 生成
- 提取所有标题和对应首段
- **用途：**
  - 目录导航
  - 章节级检索
  - Rerank 时提供章节级上下文

**L2 层（详细）：**
- RAGFlow 增强的切片内容
- 包含完整的位置信息
- **用途：**
  - 详细内容检索
  - 精确答案提取

**TODO: 分层结构用于 Rerank 优化**
- 在 Rerank 阶段，利用 L0/L1 提供的层级上下文信息
- 例如：如果查询匹配 L1 的某个章节标题，提升该章节下所有 L2 chunks 的排序
- 利用层级关系（parent_chunk_id）进行上下文扩展
- 利用 RAGFlow 的 bbox 信息判断内容重要性
- 实现基于文档结构的智能重排序

## 8. 性能优化

### 8.1 存储优化

- MySQL 只存轻量级元数据
- 大文本内容存储在 VikingFS
- 向量数据库只存向量和简单元数据

### 8.2 查询优化

- 元数据查询在 MySQL（毫秒级）
- 内容按需从 VikingFS 加载
- 向量检索返回 ID，再查 MySQL

### 8.3 批量处理

- Embedding 批量向量化（batch_size=100）
- 切片批量插入
- 向量批量写入

## 9. 安全性

### 9.1 认证与授权

- JWT Token 认证
- 基于 ACL 的权限控制
- 操作审计日志

### 9.2 数据安全

- 密码 bcrypt 加密
- 共享链接密码保护
- 敏感数据加密存储

### 9.3 访问控制

- 文件级权限检查
- 目录级权限继承
- 团队权限管理

## 10. 扩展性

### 10.1 水平扩展

- API 服务无状态，可多实例部署
- MySQL 主从复制
- 向量数据库集群部署
- 对象存储天然支持扩展

### 10.2 功能扩展

- 支持更多文档格式（通过 Parser 注册）
- 支持更多向量数据库（通过 Adapter）
- 支持更多存储后端（通过 Adapter）
- 支持更多 Embedding 模型

## 11. 开发计划

### 阶段一：环境搭建与集成（1周）
- [ ] 安装和配置 OpenViking
- [ ] 安装和配置 RAGFlow
- [ ] 搭建 MySQL 数据库
- [ ] 配置 Docker 开发环境
- [ ] 验证 OpenViking 和 RAGFlow 基础功能

### 阶段二：核心集成层（2周）
- [ ] 实现 OpenRag 核心类（集成 OpenViking）
- [ ] 实现 RAGFlow 文档处理增强层
- [ ] 实现增强切片引擎（RAGFlow + OpenViking 兼容）
- [ ] 实现文档上传流程（RAGFlow 解析 → OpenViking 存储）
- [ ] 测试基础文档处理和存储

### 阶段三：权限与用户管理（2周）
- [ ] MySQL 数据库表设计和创建
- [ ] 用户/团队管理模块
- [ ] 权限管理系统（ACL）
- [ ] 共享链接功能
- [ ] 审计日志
- [ ] 权限检查集成到文件操作

### 阶段四：检索服务（1周）
- [ ] 集成 OpenViking 的分层检索
- [ ] 实现权限过滤层
- [ ] Rerank 重排序（基础版）
- [ ] TODO: 基于层级结构和位置信息的 Rerank 优化（后续迭代）
- [ ] 检索 API 实现

### 阶段五：前端与部署（1周）
- [ ] RESTful API 完善
- [ ] Web UI（文件管理、搜索、权限配置）
- [ ] Docker Compose 部署配置
- [ ] 文档和测试
- [ ] 性能优化

**总计：约 7 周**

### 依赖关系

```
阶段一（环境）
    ↓
阶段二（核心集成）
    ↓
阶段三（权限）  ←→  阶段四（检索）
    ↓                   ↓
    └─────→  阶段五（前端部署）
```

## 12. 总结

OpenRag 通过深度集成 OpenViking 和 RAGFlow，构建了一个功能完整、架构清晰的企业级文档管理与检索系统。

### 核心架构

```
OpenRag = OpenViking (核心引擎) + RAGFlow (文档处理) + 企业功能层
```

**三层架构：**

1. **OpenViking 核心引擎层**（复用）
   - VikingFS 虚拟文件系统
   - 分层检索（L0/L1/L2）
   - 目录递归检索
   - TreeBuilder
   - 向量数据库抽象
   - 意图分析

2. **RAGFlow 文档处理增强层**（集成）
   - deepdoc 深度文档解析
   - OCR + 布局识别 + 表格识别
   - 多种切片策略
   - 完整位置信息提取

3. **OpenRag 企业功能层**（新增）
   - 用户/团队管理
   - 企业级权限控制（ACL）
   - 共享链接（密码、过期、访问控制）
   - Web UI 和 API
   - 审计日志

### 核心优势

1. **充分利用 OpenViking 的核心能力**
   - 不重复造轮子
   - 复用成熟的分层检索和目录管理
   - 继承 OpenViking 的架构优势

2. **RAGFlow 的强大文档处理能力**
   - 深度文档解析（OCR、布局、表格）
   - 多种切片策略
   - 完整的位置信息（页码、偏移、坐标、层级）
   - 比 OpenViking 默认处理更强大

3. **企业级功能完善**
   - 完整的权限体系
   - 灵活的共享机制
   - 友好的 Web UI
   - 审计和合规

4. **可插拔架构**
   - 存储后端可切换（AGFS/S3/Local）
   - 向量数据库可切换（Milvus/Qdrant/ES）
   - Embedding 模型可切换

### 技术亮点

- ✅ 深度集成 OpenViking（而非重新实现）
- ✅ RAGFlow 文档处理增强
- ✅ 保留完整位置信息用于 Rerank
- ✅ 分层结构（L0/L1/L2）的多重价值
- ✅ 企业级权限和共享
- ✅ 混合检索策略

### 与 OpenViking 的关系

| 方面 | OpenViking | OpenRag |
|------|-----------|---------|
| **定位** | AI Agent 上下文数据库 | 企业文档管理与检索系统 |
| **用户** | AI Agent | 人类用户 + 应用系统 |
| **核心能力** | 分层检索、会话管理、记忆提取 | 文档管理、权限控制、企业检索 |
| **文档处理** | 基础解析 | RAGFlow 深度处理 |
| **权限模型** | Agent 级别 | 企业级 ACL |
| **关系** | - | **基于 OpenViking 构建** |

### 开发优势

通过深度集成 OpenViking：
- 减少开发时间（从 6-7 周优化到 7 周，但功能更强大）
- 代码质量更高（复用成熟组件）
- 维护成本更低（OpenViking 更新自动受益）
- 架构更清晰（职责分离）

### 未来扩展

- 支持更多文档格式（通过 RAGFlow）
- 更智能的 Rerank（利用位置信息和层级结构）
- 多语言支持
- 更丰富的权限策略
- 与企业系统集成（LDAP、SSO）
