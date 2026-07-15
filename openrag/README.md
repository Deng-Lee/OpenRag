# 🚀 OpenRag

OpenRag 是一个基于 **OpenViking** 和 **RAGFlow** 构建的企业级文档管理与智能检索系统。它不仅提供了强大的 RAG（检索增强生成）底层能力，还为企业级 B2B SaaS 场景量身打造了多租户物理隔离、严密的权限管控、高性能异步文档处理流水线以及灵活的团队协作功能。

## ✨ 核心特性

- 🏢 **多租户架构与物理隔离 (Workspace)**
  采用工作空间（Workspace）作为多租户隔离的基座。底层深度整合 MinIO 对象存储，每个工作空间自动映射为独立的物理 Bucket，从根源上保障企业数据的绝对隔离与安全。
- 🔒 **企业级权限管控 (Workspace RBAC)**
  以工作空间成员和角色的 `read` / `write` 权限作为文件访问边界，并提供独立的分享链接与服务令牌能力；全局操作审计日志 (AuditLog) 用于追踪关键操作。
- ⚡ **高性能异步处理流水线**
  集成 Celery 构建分布式任务调度引擎。文档上传后无缝进入后台流水线，支持智能优先级队列调度，包含完整生命周期：`解析 (Parsing)` -> `切片 (Chunking)` -> `树状层级构建 (Hierarchy L0/L1/L2)` -> `向量化 (Embedding)`。
- 📂 **全功能虚拟文件系统 (VikingFS)**
  提供无限级文件夹嵌套、文件移动、重命名及空间内全文检索能力，为用户提供类似本地文件系统的流畅体验。
- 🔗 **安全的企业文件外发与共享**
  支持生成带密码提取码、访问次数限制以及过期时间的安全分享链接 (ShareLink)。

## 🏗️ 系统架构与主要模块

系统采用现代化的 Python 微服务架构，核心模块分布如下：

- **`api/` (API 接口层)**: 基于 FastAPI 构建的 RESTful 接口，涵盖认证 (Auth)、文件管理 (Files)、工作空间 (Workspaces)、权限控制 (Permissions) 及任务查询 (Tasks) 等。
- **`models/` (数据模型层)**: 基于 SQLAlchemy 的关系型数据模型。
- **`storage/` (物理存储层)**: 封装的 `MinioStorage` 客户端，实现文件路径到 MinIO 存储桶的透明转换与管理。
- **`tasks/` (异步任务调度)**: 承载 Celery worker，负责大文件的离线切片与向量化计算，通过 Redis 协调并发控制与限流 (Quota)。
- **`processors/` & `parsers/` (文档处理引擎)**: 深度集成 RAGFlow 的 `deepdoc`，具备强大的视觉与版面分析能力，支持 PDF, Word, Excel, PPT, HTML, Markdown 等多格式高精度解析。
- **`hierarchy/` & `chunking/` (RAG 增强层)**: 负责将文档切分为智能语义块，并构建 L0(全文摘要)/L1(章节预览)/L2(切片细节) 的多维度检索树结构。

## 🗄️ 核心数据模型 (Data Models)

系统的数据模型围绕“多租户”、“文件资源”与“任务追踪”三大维度设计：

1. **`Workspace` (工作空间)**: 系统的顶级租户容器，定义了空间存储配额与并发计算能力。
2. **`User` & `Team`**: 系统的身份主体。用户可以加入多个工作空间，并在空间内通过团队进行协同；团队不直接授予文件访问权限。
3. **`File` (文件与目录)**: 虚拟文件节点。不仅记录基础元数据，还深度绑定了 RAG 属性（如 `processing_status`, `total_chunks`, `parser_type`）。
4. **`Task` (异步任务)**: 记录文档切片与 Embedding 任务的实时执行进度（`progress`）、状态及错误日志。
5. **`AuditLog` (审计日志)**: 详细记录系统内所有的“增删改查”及“分享”行为。

## 🛠️ 技术栈

- **后端框架**: Python 3.10+, FastAPI
- **关系型数据库**: MySQL 8.0 (SQLAlchemy + Alembic)
- **向量数据库**: Milvus Standalone
- **对象存储**: MinIO
- **缓存与队列**: Redis, Celery
- **AI 核心组件**: OpenViking, RAGFlow (deepdoc 组件)

## 🚀 快速开始

详见 [开发指南](docs/development.md)

### 1. 启动开发环境

```bash
cd docker
docker-compose up -d
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 验证环境

```bash
./scripts/verify_environment.sh
```

### 4. 运行测试

```bash
pytest
```

## 📖 文档资源

- [本地开发与部署指南](../LOCAL_DEV_GUIDE.md)
- [系统核心设计规范](docs/superpowers/specs/2026-04-07-openrag-design.md)
- [实现计划](docs/superpowers/plans/)

## 📄 License

Apache 2.0
