# OpenRag 本地开发与启动指南

本文档将指导您如何在本地环境启动 OpenRag 的完整开发栈，包括基础设施（数据库、缓存等）、Python 后端 API 以及 React/Vite 前端服务。

## 📋 环境要求

在开始之前，请确保您的本地环境已安装以下依赖：
- **Docker & Docker Compose** (用于启动 PostgreSQL, Milvus, MinIO, Elasticsearch 等基础设施)
- **Python 3.10+** (用于运行后端 FastAPI 服务)
- **Node.js 18+ & npm** (用于运行前端 Web 服务)
- **Git**

---

## 🚀 第一步：启动基础设施 (依赖服务)

OpenRag 依赖于多个外部服务。我们提供了 `docker-compose.dev.yml` 以一键启动这些服务。

1. 打开终端，进入 `docker` 目录：
   ```bash
   cd docker
   ```
2. 复制环境变量模板（可选，使用默认配置即可）：
   ```bash
   cp .env.example .env
   ```
3. 启动开发环境的 Docker 容器：
   ```bash
   docker-compose -f docker-compose.dev.yml up -d
   docker compose -f docker-compose.dev.yml up -d postgres etcd minio milvus elasticsearch
   ```
4. 检查服务是否全部正常运行（看到 `Up` 状态即可）：
   ```bash
   docker-compose -f docker-compose.dev.yml ps
   ```
   *启动的服务包括：PostgreSQL(5432), MinIO(9000/9001), Milvus(19530), Etcd(2379), Elasticsearch(9200)*。

> ⚠️ **注意**：文档解析需要 Task Worker，请参考第四步启动。

---

## 🐍 第二步：启动 Python 后端 API

后端 API 位于 `openrag/` 目录下，基于 FastAPI 构建。

1. 新开一个终端窗口，进入后端目录：
   ```bash
   cd openrag
   ```
2. 创建并激活 Python 虚拟环境：
   - **Windows (PowerShell)**:
     ```powershell
     python -m venv venv
     .\venv\Scripts\Activate.ps1
     ```
   - **macOS/Linux**:
     ```bash
     python -m venv venv
     source venv/bin/activate
     ```
3. 安装依赖包：
   ```bash
   pip install -r requirements.txt
   ```
4. 配置环境变量（复制示例配置）：
   ```bash
   cp .env.example .env
   ```
   在 **`openrag/.env`**（或你加载的环境变量）中配置 **PostgreSQL**，且必须与 **`docker/.env`** 里 Postgres 容器使用的账号一致，否则会出现 `password authentication failed for user "..."`：
   ```env
   POSTGRES_HOST=127.0.0.1
   POSTGRES_PORT=5432
   POSTGRES_USER=openrag
   POSTGRES_PASSWORD=openrag_postgres_password_2024
   POSTGRES_DATABASE=openrag
   ```
   若未改 `docker/docker-compose.dev.yml` 的默认值，则用户名/库名一般为 **`openrag`**，密码为 **`openrag_pass`**（与 `docker/.env` 中 `POSTGRES_*` 一致）。**不要**在应用里写 Docker 未创建的用户名（例如仅在后端配置了 `superman` 而数据库里仍是 `openrag`）。若曾用错误密码初始化过数据卷，可执行第一步文末的「清理与重置」后重新 `up -d`。
5. 启动后端服务：
   ```bash
   python run_api.py
   ```
   > **成功标志**：终端显示 `Uvicorn running on http://0.0.0.0:8001`。
   > 您可以通过访问 [http://localhost:8001/docs](http://localhost:8001/docs) 查看交互式 API 文档。

---

## 💻 第三步：启动前端 Web 界面

前端代码位于 `web/` 目录下，基于 React 和 Vite 构建。

1. 再新开一个终端窗口，进入前端目录：
   ```bash
   cd web
   ```
2. 配置环境变量（复制示例配置）：
   ```bash
   cp .env.example .env
   ```
3. 安装 Node 依赖：
   ```bash
   npm install
   ```
4. 启动前端开发服务器：
   ```bash
   npm run dev
   ```
   > **成功标志**：终端显示类似 `Vite ready in XXX ms`，并提示可通过 `http://localhost:3000/` 访问（端口以 `web/vite.config.ts` 中 `server.port` 为准）。

---

## 🛠️ 常用开发命令备忘

### 停止基础设施服务
如果您想停止后台的 Docker 容器释放资源：
```bash
cd docker
docker-compose -f docker-compose.dev.yml down
docker-compose -f docker-compose.worker.yml down
```

### 清理与重置数据库数据
如果需要清空并重置所有本地数据库和向量库数据：
```bash
cd docker
docker-compose -f docker-compose.dev.yml down -v
docker-compose -f docker-compose.worker.yml down -v
```

## 🤖 第四步：启动 Task Worker（文档解析）

文档上传后需要 Task Worker 从数据库拉取任务进行异步解析处理。

### 本地运行（开发调试）

```bash
cd openrag
source venv/bin/activate  # Windows: venv\Scripts\activate

# 启动 Task Worker（默认 4 个进程）
python -m openrag.worker.task_worker

# 指定进程数和 API 地址
python -m openrag.worker.task_worker --num-workers 1 --api-url http://localhost:8001

# 查看所有选项
python -m openrag.worker.task_worker --help
```

Worker 会自动轮询 `/broker/get-tasks` 端点，根据动态权重分配任务。支持进程崩溃后自动重启。

### 使用 Docker 部署

```bash
cd docker

# 启动基础设施
docker-compose -f docker-compose.dev.yml up -d

# 启动 Task Worker（连接 localhost:8001，假设 API 在主机上运行）
WORKER_API_URL=http://host.docker.internal:8001 docker-compose -f docker-compose.worker.yml up -d

# 或者在生产环境使用完整 stack（API + Worker 都在 Docker 内）
docker-compose -f docker-compose.prod.yml up -d

# 查看 Worker 日志
docker logs -f openrag-task-worker
```

### 查看任务队列状态

```bash
# 查看任务列表（将 1 换成实际 workspace id）
curl http://localhost:8001/workspaces/1/tasks

# 查看各 workspace 的调度权重
curl http://localhost:8001/broker/weights
```

### 运行后端测试

仓库内 `openrag/pytest.ini` 通过 `python_files` **只收集部分契约用例**；默认在项目根执行 `pytest` 时，可能**不会**跑全部测试文件。开发时建议显式指定路径。

```bash
cd openrag

# RAGFlow 同构 parity（推荐：解析路由、compat、语义切片、metadata、编排参数）
python -m pytest tests/parity -v

# parity + chunk 引擎回归
python -m pytest tests/parity tests/test_chunk_engine.py -q

# 契约子集（与 pytest.ini 中 python_files 一致；用于 CI/稳定接口）
python -m pytest tests/test_service_api.py -v
```

---

## Windows 本地启动速查

下面这组命令适合在 Windows PowerShell 中启动当前本地开发环境。

### 1. 启动 Docker 依赖

先启动后端依赖的基础服务。至少需要 PostgreSQL 和 MinIO；如果要跑完整文档处理、向量检索或全文检索，也一起启动 Milvus、Etcd、Elasticsearch。

```powershell
cd E:\project\OpenRag\docker
docker compose -f docker-compose.dev.yml up -d postgres etcd minio milvus elasticsearch
docker compose -f docker-compose.dev.yml ps
```

如果只是验证页面和基础文件接口，PostgreSQL + MinIO 通常是最低要求：

```powershell
cd E:\project\OpenRag\docker
docker compose -f docker-compose.dev.yml up -d postgres minio
```

### 2. 迁移真实数据库

在后端目录执行 Alembic 迁移。看到 `Context impl PostgresqlImpl` 且命令正常返回，通常表示已经迁移到最新；也可以用 `current` 确认版本。

```powershell
cd E:\project\OpenRag\openrag
python -c "from alembic.config import main; main(argv=['upgrade','head'])"
python -c "from alembic.config import main; main(argv=['current'])"
```

### 3. 启动后端 API

```powershell
cd E:\project\OpenRag\openrag
$env:PYTHONPATH = "src;."
$env:PORT = "8001"
python run_api.py
```

后端健康检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8001/health
```

### 4. 启动前端

另开一个 PowerShell 窗口：

```powershell
cd E:\project\OpenRag\web
$env:VITE_API_URL = "http://127.0.0.1:8001"
npm run dev -- --host 127.0.0.1 --port 3000
```

然后访问：

```text
http://127.0.0.1:3000
```

### 5. 可选：启动 Task Worker

上传文档后，如果希望文档自动解析、切块和入库，需要额外启动 worker。

```powershell
cd E:\project\OpenRag\openrag
$env:PYTHONPATH = "src;."
python -m openrag.worker.task_worker --num-workers 1 --api-url http://127.0.0.1:8001
```

### 6. 端口占用处理

如果 `8001` 或 `3000` 已被占用，先找正在监听的 PID：

```powershell
netstat -ano | Select-String ":8001" | Select-String "LISTENING"
netstat -ano | Select-String ":3000" | Select-String "LISTENING"
```

停止对应进程：

```powershell
Stop-Process -Id <PID> -Force
```

注意：`TIME_WAIT` 且 PID 为 `0` 的行不是正在运行的服务，不需要停止。

Parity 用例依赖 `pytest` 的 `pythonpath`（已配置为仓库根目录与 `src/`），以便加载 `openrag` 与 `common`。可选黄金样本说明见 `openrag/tests/parity/fixtures/README.md`。
