# OpenRag 生产部署速查（Docker Compose）

与当前 `docker-compose.prod.yml` 保持一致：**Task Worker** 负责文档解析队列；**无 Celery**。API 在容器内监听 **8000**；默认将宿主机 **8001** 映射到该端口。

## 初始化

```bash
cd docker
cp .env.example .env
# 编辑 .env：POSTGRES_PASSWORD、SECRET_KEY、MINIO_ROOT_PASSWORD、CORS_ORIGINS、OPENAI_API_KEY 等
./deploy.sh
```

部署前请在 `web/` 执行 `npm install && npm run build`（若 `deploy.sh` 未自动完成）。

## 常用命令

### 启停

```bash
docker-compose -f docker-compose.prod.yml up -d
docker-compose -f docker-compose.prod.yml down
docker-compose -f docker-compose.prod.yml restart
```

### 日志

```bash
docker-compose -f docker-compose.prod.yml logs -f
docker-compose -f docker-compose.prod.yml logs -f api
docker-compose -f docker-compose.prod.yml logs -f web
docker-compose -f docker-compose.prod.yml logs -f task-worker
```

### 状态

```bash
docker-compose -f docker-compose.prod.yml ps
docker stats
```

### 升级

```bash
docker-compose -f docker-compose.prod.yml build api
docker-compose -f docker-compose.prod.yml up -d api

cd ../web && npm run build && cd ../docker
docker-compose -f docker-compose.prod.yml build web
docker-compose -f docker-compose.prod.yml up -d web

docker-compose -f docker-compose.prod.yml build task-worker
docker-compose -f docker-compose.prod.yml up -d task-worker
```

### 数据库迁移

```bash
docker-compose -f docker-compose.prod.yml exec api alembic upgrade head
```

### PostgreSQL 备份 / 恢复

```bash
docker-compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U openrag openrag > backup.sql

docker-compose -f docker-compose.prod.yml exec -T postgres \
  psql -U openrag -d openrag < backup.sql
```

### 文档解析（Task Worker）

- 服务名：`task-worker`（镜像由 `docker/Dockerfile.worker` 构建）。
- 可通过环境变量 `WORKER_NUM`、`WORKER_POLL_INTERVAL` 等调整并发（见 `docker-compose.prod.yml`）。
- 当前 compose 为 `task-worker` 指定了 `container_name`，**不支持** `docker compose up --scale task-worker=N`；需要更高吞吐时请提高 `WORKER_NUM` 或拆分为多套部署。

## 排障

```bash
docker-compose -f docker-compose.prod.yml ps
# 直连宿主机上的 API 映射端口
curl -sS http://localhost:8001/health
# 经 Nginx 同源代理（浏览器与生产前端一致）
curl -sS http://localhost/api/health
curl -sS http://localhost/health
```

进入容器：

```bash
docker-compose -f docker-compose.prod.yml exec api bash
```

## 访问地址（默认端口）

| 用途 | URL |
|------|-----|
| 前端（Nginx） | http://localhost |
| API（宿主机直连，映射端口） | http://localhost:8001 |
| OpenAPI | http://localhost:8001/docs 或 http://localhost/api/docs（经 `/api` 前缀代理） |
| Milvus MinIO 控制台 | http://localhost:9001 |

## 环境变量（`.env`）

必填/强烈建议：`POSTGRES_PASSWORD`、`SECRET_KEY`、`MINIO_ROOT_PASSWORD`、`CORS_ORIGINS`、`OPENAI_API_KEY`。

与全文检索相关：`ELASTICSEARCH__ENABLED`、`ELASTICSEARCH__HOSTS`（compose 内默认 `http://elasticsearch:9200`）。详见 `docker/.env.example`。
