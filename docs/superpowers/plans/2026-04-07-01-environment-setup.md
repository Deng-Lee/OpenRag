# 计划 1：环境搭建与基础集成

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 OpenViking + RAGFlow 开发环境，验证基础功能，创建项目骨架

**Architecture:** 基于 Docker Compose 的开发环境，集成 OpenViking 和 RAGFlow，配置 MySQL、Redis、Milvus 等依赖服务

**Tech Stack:** Docker, Docker Compose, OpenViking, RAGFlow, MySQL 8.0, Redis, Milvus

**Dependencies:** 无（这是第一个计划）

**Priority:** P0（必须首先完成）

---

## 文件结构

```
openrag/
├── docker/
│   ├── docker-compose.yml          # Docker Compose 配置
│   ├── mysql/
│   │   └── init.sql                # MySQL 初始化脚本
│   └── config/
│       ├── openviking.yaml         # OpenViking 配置
│       └── ragflow.yaml            # RAGFlow 配置
├── src/
│   └── openrag/
│       ├── __init__.py
│       └── config.py               # 配置管理
├── tests/
│   └── test_environment.py         # 环境验证测试
├── requirements.txt                # Python 依赖
└── README.md                       # 项目文档
```

---

### Task 1: 创建项目目录结构

**Files:**
- Create: `docker/docker-compose.yml`
- Create: `docker/mysql/init.sql`
- Create: `src/openrag/__init__.py`
- Create: `requirements.txt`
- Create: `README.md`

- [ ] **Step 1: 创建基础目录结构**

```bash
mkdir -p docker/mysql docker/config
mkdir -p src/openrag
mkdir -p tests
```

- [ ] **Step 2: 创建 Python 包初始化文件**

Create `src/openrag/__init__.py`:
```python
"""
OpenRag - 基于 OpenViking 的企业文档管理与检索系统
"""

__version__ = "0.1.0"
```

- [ ] **Step 3: 创建 requirements.txt**

Create `requirements.txt`:
```txt
# OpenViking
openviking>=0.1.0

# RAGFlow 依赖
deepdoc>=0.1.0
pillow>=10.0.0

# Web 框架
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
pydantic>=2.0.0
pydantic-settings>=2.0.0

# 数据库
sqlalchemy>=2.0.0
pymysql>=1.1.0
alembic>=1.12.0

# 异步任务
celery>=5.3.0
redis>=5.0.0

# 向量数据库客户端
pymilvus>=2.3.0
qdrant-client>=1.6.0

# 工具
python-multipart>=0.0.6
python-jose[cryptography]>=3.3.0
passlib[bcrypt]>=1.7.4
python-dotenv>=1.0.0
pyyaml>=6.0

# 测试
pytest>=7.4.0
pytest-asyncio>=0.21.0
pytest-cov>=4.1.0
httpx>=0.25.0
```

- [ ] **Step 4: 创建 README.md**

Create `README.md`:
```markdown
# OpenRag

基于 OpenViking 和 RAGFlow 的企业文档管理与检索系统

## 架构

- **OpenViking**: 核心引擎（VikingFS、分层检索、TreeBuilder）
- **RAGFlow**: 文档处理增强（deepdoc、切片引擎）
- **OpenRag**: 企业功能层（权限、共享、Web UI）

## 快速开始

### 1. 启动开发环境

```bash
cd docker
docker-compose up -d
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 运行测试

```bash
pytest tests/
```

## 开发计划

1. ✅ 环境搭建与基础集成
2. ⏳ 权限管理系统
3. ⏳ 文档处理系统
4. ⏳ 检索服务
5. ⏳ API 和前端
```

- [ ] **Step 5: 提交基础结构**

```bash
git add .
git commit -m "chore: initialize project structure

- Create directory structure
- Add requirements.txt
- Add README.md"
```

---

### Task 2: 配置 Docker Compose 环境

**Files:**
- Create: `docker/docker-compose.yml`
- Create: `docker/mysql/init.sql`
- Create: `docker/config/openviking.yaml`

- [ ] **Step 1: 创建 Docker Compose 配置**

Create `docker/docker-compose.yml`:
```yaml
version: '3.8'

services:
  # MySQL 数据库
  mysql:
    image: mysql:8.0
    container_name: openrag-mysql
    environment:
      MYSQL_ROOT_PASSWORD: openrag_dev_password
      MYSQL_DATABASE: openrag
    ports:
      - "3306:3306"
    volumes:
      - mysql_data:/var/lib/mysql
      - ./mysql/init.sql:/docker-entrypoint-initdb.d/init.sql
    networks:
      - openrag-network
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Redis (任务队列)
  redis:
    image: redis:7-alpine
    container_name: openrag-redis
    ports:
      - "6379:6379"
    networks:
      - openrag-network
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Milvus Standalone
  milvus-etcd:
    image: quay.io/coreos/etcd:v3.5.5
    container_name: openrag-milvus-etcd
    environment:
      - ETCD_AUTO_COMPACTION_MODE=revision
      - ETCD_AUTO_COMPACTION_RETENTION=1000
      - ETCD_QUOTA_BACKEND_BYTES=4294967296
      - ETCD_SNAPSHOT_COUNT=50000
    command: etcd -advertise-client-urls=http://127.0.0.1:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
    networks:
      - openrag-network

  milvus-minio:
    image: minio/minio:RELEASE.2023-03-20T20-16-18Z
    container_name: openrag-milvus-minio
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "9001:9001"
      - "9000:9000"
    command: minio server /minio_data --console-address ":9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 30s
      timeout: 20s
      retries: 3
    networks:
      - openrag-network

  milvus:
    image: milvusdb/milvus:v2.3.3
    container_name: openrag-milvus
    environment:
      ETCD_ENDPOINTS: milvus-etcd:2379
      MINIO_ADDRESS: milvus-minio:9000
    ports:
      - "19530:19530"
      - "9091:9091"
    depends_on:
      - milvus-etcd
      - milvus-minio
    networks:
      - openrag-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 30s
      timeout: 20s
      retries: 3

volumes:
  mysql_data:
  milvus_data:

networks:
  openrag-network:
    driver: bridge
```

- [ ] **Step 2: 创建 MySQL 初始化脚本**

Create `docker/mysql/init.sql`:
```sql
-- OpenRag 数据库初始化脚本

-- 创建数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS openrag CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE openrag;

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) PRIMARY KEY,
    username VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_username (username),
    INDEX idx_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 插入测试用户
INSERT INTO users (id, username, email, password_hash, is_admin) VALUES
('admin', 'admin', 'admin@openrag.local', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYqVr/VXlTW', TRUE);
-- 密码: admin123

-- 验证表
SELECT 'Database initialized successfully' AS status;
```

- [ ] **Step 3: 创建 OpenViking 配置文件**

Create `docker/config/openviking.yaml`:
```yaml
# OpenViking 配置

storage:
  type: local
  config:
    base_path: /data/openviking

vector_db:
  type: milvus
  config:
    host: milvus
    port: 19530

embedding:
  model: text-embedding-3-small
  provider: openai
  api_key: ${OPENAI_API_KEY}
  dimension: 1536

collections:
  default:
    dimension: 1536
    metric: cosine
```

- [ ] **Step 4: 启动 Docker 环境**

```bash
cd docker
docker-compose up -d
```

Expected output:
```
Creating network "openrag-network" with driver "bridge"
Creating openrag-mysql ... done
Creating openrag-redis ... done
Creating openrag-milvus-etcd ... done
Creating openrag-milvus-minio ... done
Creating openrag-milvus ... done
```

- [ ] **Step 5: 验证服务状态**

```bash
docker-compose ps
```

Expected: All services should be "Up" and healthy

- [ ] **Step 6: 提交 Docker 配置**

```bash
git add docker/
git commit -m "chore: add docker compose configuration

- Add MySQL, Redis, Milvus services
- Add MySQL init script
- Add OpenViking config"
```

---

### Task 3: 安装和验证 OpenViking

**Files:**
- Create: `tests/test_openviking.py`
- Create: `src/openrag/config.py`

- [ ] **Step 1: 安装 OpenViking**

```bash
pip install openviking
```

Expected: Successfully installed openviking and dependencies

- [ ] **Step 2: 创建配置管理模块**

Create `src/openrag/config.py`:
```python
"""配置管理模块"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class StorageConfig(BaseSettings):
    """存储配置"""
    type: str = "local"
    base_path: str = "/tmp/openrag"
    endpoint: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    bucket: Optional[str] = None


class VectorDBConfig(BaseSettings):
    """向量数据库配置"""
    type: str = "milvus"
    host: str = "localhost"
    port: int = 19530


class MySQLConfig(BaseSettings):
    """MySQL 配置"""
    host: str = "localhost"
    port: int = 3306
    database: str = "openrag"
    user: str = "root"
    password: str = "openrag_dev_password"


class EmbeddingConfig(BaseSettings):
    """Embedding 配置"""
    model: str = "text-embedding-3-small"
    provider: str = "openai"
    api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    dimension: int = 1536


class Config(BaseSettings):
    """全局配置"""
    storage: StorageConfig = Field(default_factory=StorageConfig)
    vector_db: VectorDBConfig = Field(default_factory=VectorDBConfig)
    mysql: MySQLConfig = Field(default_factory=MySQLConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "Config":
        """从 YAML 文件加载配置"""
        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)
        return cls(**data)


# 全局配置实例
config = Config()
```

- [ ] **Step 3: 编写 OpenViking 验证测试**

Create `tests/test_openviking.py`:
```python
"""OpenViking 集成测试"""

import pytest


def test_import_openviking():
    """测试 OpenViking 导入"""
    try:
        import openviking
        assert hasattr(openviking, 'OpenViking')
        assert hasattr(openviking, 'VikingFS')
    except ImportError as e:
        pytest.fail(f"Failed to import OpenViking: {e}")


def test_openviking_version():
    """测试 OpenViking 版本"""
    import openviking
    assert hasattr(openviking, '__version__')
    print(f"OpenViking version: {openviking.__version__}")


@pytest.mark.skip(reason="Requires OpenViking installation and configuration")
def test_openviking_initialization():
    """测试 OpenViking 初始化"""
    from openviking import OpenViking
    from src.openrag.config import config
    
    # 初始化 OpenViking
    viking = OpenViking(
        storage_config={
            'type': config.storage.type,
            'config': {
                'base_path': config.storage.base_path
            }
        },
        vector_db_config={
            'type': config.vector_db.type,
            'config': {
                'host': config.vector_db.host,
                'port': config.vector_db.port
            }
        }
    )
    
    assert viking is not None
    assert hasattr(viking, 'get_viking_fs')
    
    # 测试 VikingFS
    viking_fs = viking.get_viking_fs()
    assert viking_fs is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 4: 运行测试**

```bash
pytest tests/test_openviking.py -v
```

Expected output:
```
tests/test_openviking.py::test_import_openviking PASSED
tests/test_openviking.py::test_openviking_version PASSED
tests/test_openviking.py::test_openviking_initialization SKIPPED
```

- [ ] **Step 5: 提交代码**

```bash
git add src/openrag/config.py tests/test_openviking.py
git commit -m "feat: add config management and openviking tests

- Add Config class with pydantic settings
- Add OpenViking integration tests
- Verify OpenViking installation"
```

---

### Task 4: 安装和验证 RAGFlow

**Files:**
- Create: `tests/test_ragflow.py`

- [ ] **Step 1: 安装 RAGFlow 依赖**

```bash
pip install deepdoc pillow
```

Expected: Successfully installed RAGFlow dependencies

- [ ] **Step 2: 编写 RAGFlow 验证测试**

Create `tests/test_ragflow.py`:
```python
"""RAGFlow 集成测试"""

import pytest


def test_import_deepdoc():
    """测试 deepdoc 导入"""
    try:
        from deepdoc.parser import PdfParser
        assert PdfParser is not None
    except ImportError as e:
        pytest.fail(f"Failed to import deepdoc: {e}")


def test_import_rag_nlp():
    """测试 rag.nlp 导入"""
    try:
        from rag.nlp import rag_tokenizer
        assert rag_tokenizer is not None
    except ImportError as e:
        pytest.fail(f"Failed to import rag.nlp: {e}")


@pytest.mark.skip(reason="Requires test PDF file")
def test_pdf_parser():
    """测试 PDF 解析器"""
    from deepdoc.parser import PdfParser
    
    parser = PdfParser()
    assert parser is not None
    
    # TODO: 添加实际的 PDF 解析测试
    # boxes, tables = parser("test.pdf")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 3: 运行测试**

```bash
pytest tests/test_ragflow.py -v
```

Expected output:
```
tests/test_ragflow.py::test_import_deepdoc PASSED
tests/test_ragflow.py::test_import_rag_nlp PASSED
tests/test_ragflow.py::test_pdf_parser SKIPPED
```

- [ ] **Step 4: 提交代码**

```bash
git add tests/test_ragflow.py
git commit -m "feat: add ragflow integration tests

- Add deepdoc import tests
- Add rag.nlp import tests
- Verify RAGFlow installation"
```

---

### Task 5: 创建环境验证脚本

**Files:**
- Create: `tests/test_environment.py`
- Create: `scripts/verify_environment.sh`

- [ ] **Step 1: 编写完整的环境验证测试**

Create `tests/test_environment.py`:
```python
"""环境验证测试"""

import pytest
import socket


def test_mysql_connection():
    """测试 MySQL 连接"""
    try:
        import pymysql
        conn = pymysql.connect(
            host='localhost',
            port=3306,
            user='root',
            password='openrag_dev_password',
            database='openrag'
        )
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        assert result == (1,)
        conn.close()
    except Exception as e:
        pytest.fail(f"MySQL connection failed: {e}")


def test_redis_connection():
    """测试 Redis 连接"""
    try:
        import redis
        r = redis.Redis(host='localhost', port=6379, decode_responses=True)
        r.ping()
        assert r.ping() is True
    except Exception as e:
        pytest.fail(f"Redis connection failed: {e}")


def test_milvus_connection():
    """测试 Milvus 连接"""
    try:
        from pymilvus import connections, utility
        connections.connect(host='localhost', port=19530)
        assert connections.has_connection('default')
        connections.disconnect('default')
    except Exception as e:
        pytest.fail(f"Milvus connection failed: {e}")


def test_all_services_running():
    """测试所有服务是否运行"""
    services = [
        ('MySQL', 'localhost', 3306),
        ('Redis', 'localhost', 6379),
        ('Milvus', 'localhost', 19530),
    ]
    
    for name, host, port in services:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        assert result == 0, f"{name} is not running on {host}:{port}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 创建验证脚本**

Create `scripts/verify_environment.sh`:
```bash
#!/bin/bash

echo "=== OpenRag Environment Verification ==="
echo ""

# 检查 Docker 服务
echo "1. Checking Docker services..."
cd docker
docker-compose ps
echo ""

# 检查 Python 依赖
echo "2. Checking Python dependencies..."
pip list | grep -E "openviking|deepdoc|fastapi|sqlalchemy|pymilvus"
echo ""

# 运行测试
echo "3. Running environment tests..."
cd ..
pytest tests/test_environment.py -v
echo ""

echo "=== Verification Complete ==="
```

- [ ] **Step 3: 添加执行权限**

```bash
chmod +x scripts/verify_environment.sh
```

- [ ] **Step 4: 运行验证脚本**

```bash
./scripts/verify_environment.sh
```

Expected: All tests should pass

- [ ] **Step 5: 提交代码**

```bash
git add tests/test_environment.py scripts/verify_environment.sh
git commit -m "feat: add environment verification

- Add comprehensive environment tests
- Add verification script
- Test MySQL, Redis, Milvus connections"
```

---

### Task 6: 创建开发文档

**Files:**
- Create: `docs/development.md`
- Update: `README.md`

- [ ] **Step 1: 创建开发文档**

Create `docs/development.md`:
```markdown
# 开发指南

## 环境要求

- Python 3.10+
- Docker & Docker Compose
- Git

## 快速开始

### 1. 克隆项目

```bash
git clone <repository-url>
cd OpenRag
```

### 2. 启动开发环境

```bash
cd docker
docker-compose up -d
```

### 3. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 4. 验证环境

```bash
./scripts/verify_environment.sh
```

## 开发工作流

### 运行测试

```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_openviking.py -v

# 运行测试并生成覆盖率报告
pytest --cov=src/openrag --cov-report=html
```

### 代码风格

```bash
# 格式化代码
black src/ tests/

# 检查代码风格
flake8 src/ tests/

# 类型检查
mypy src/
```

## 服务管理

### 启动服务

```bash
cd docker
docker-compose up -d
```

### 停止服务

```bash
docker-compose down
```

### 查看日志

```bash
docker-compose logs -f [service-name]
```

### 重启服务

```bash
docker-compose restart [service-name]
```

## 数据库管理

### 连接 MySQL

```bash
docker exec -it openrag-mysql mysql -u root -p
# Password: openrag_dev_password
```

### 查看数据库

```sql
USE openrag;
SHOW TABLES;
```

## 故障排查

### 服务无法启动

1. 检查端口占用：`netstat -an | grep <port>`
2. 查看服务日志：`docker-compose logs <service>`
3. 重启服务：`docker-compose restart <service>`

### 测试失败

1. 确保所有服务正在运行
2. 检查配置文件
3. 查看详细错误信息：`pytest -vv`
```

- [ ] **Step 2: 更新 README.md**

Update `README.md`:
```markdown
# OpenRag

基于 OpenViking 和 RAGFlow 的企业文档管理与检索系统

## 架构

- **OpenViking**: 核心引擎（VikingFS、分层检索、TreeBuilder）
- **RAGFlow**: 文档处理增强（deepdoc、切片引擎）
- **OpenRag**: 企业功能层（权限、共享、Web UI）

## 快速开始

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

## 项目结构

```
openrag/
├── docker/              # Docker 配置
├── src/openrag/         # 源代码
├── tests/               # 测试
├── docs/                # 文档
└── scripts/             # 脚本
```

## 开发计划

1. ✅ 环境搭建与基础集成
2. ⏳ 权限管理系统
3. ⏳ 文档处理系统
4. ⏳ 检索服务
5. ⏳ API 和前端

## 文档

- [开发指南](docs/development.md)
- [系统设计](docs/superpowers/specs/2026-04-07-openrag-design.md)
- [实现计划](docs/superpowers/plans/)

## License

Apache 2.0
```

- [ ] **Step 3: 提交文档**

```bash
git add docs/development.md README.md
git commit -m "docs: add development guide

- Add comprehensive development guide
- Update README with project structure
- Add troubleshooting section"
```

---

## 验收标准

完成此计划后，应该具备：

- ✅ Docker Compose 环境正常运行（MySQL、Redis、Milvus）
- ✅ OpenViking 成功安装并可导入
- ✅ RAGFlow 成功安装并可导入
- ✅ 所有环境测试通过
- ✅ 项目结构清晰，文档完整
- ✅ 可以开始后续开发工作

## 下一步

完成此计划后，继续执行：
- **计划 2：权限管理系统**
