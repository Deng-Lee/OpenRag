# OpenRag Docker Production Deployment

This directory contains Docker configuration for deploying OpenRag in production.

## Architecture

The production deployment consists of the following services:

- **web**: Nginx serving the React frontend and proxying API requests
- **api**: FastAPI backend application
- **postgres**: PostgreSQL 16 database
- **milvus**: Vector database for embeddings
- **milvus-etcd**: etcd for Milvus metadata
- **milvus-minio**: MinIO for Milvus object storage
- **elasticsearch**: Full-text chunk index (per workspace slug; optional via `ELASTICSEARCH__ENABLED`)
- **task-worker**: 从 Broker 拉取解析任务并写回向量库 / ES 等（`docker/Dockerfile.worker`）

## Prerequisites

- Docker Engine 20.10+
- Docker Compose 2.0+
- At least 4GB RAM available
- 20GB disk space

## Quick Start

### 1. Configure Environment Variables

```bash
cd docker
cp .env.example .env
```

Edit `.env` and set secure values for:
- `POSTGRES_PASSWORD`: Strong password for PostgreSQL (user `openrag`, database `openrag`)
- `SECRET_KEY`: Random 32+ character string for JWT tokens
- `MINIO_ROOT_PASSWORD`: Password for MinIO
- `CORS_ORIGINS`: Your domain(s), e.g., `http://yourdomain.com`

### 2. Build Frontend

Before deploying, build the frontend:

```bash
cd ../web
npm install
npm run build
cd ../docker
```

### 3. Start Services

```bash
docker-compose -f docker-compose.prod.yml up -d
```

### 4. Initialize Database

Wait for services to be healthy (30-60 seconds), then run migrations:

```bash
docker-compose -f docker-compose.prod.yml exec api alembic upgrade head
```

### 5. Access the Application

- Frontend: http://localhost（Nginx 静态资源 + `/api/*` 反代到 API 容器 **8000** 端口）
- API（宿主机直连 compose 映射）: http://localhost:8001
- API Docs: http://localhost:8001/docs，或经网关同源访问 http://localhost/api/docs
- MinIO Console: http://localhost:9001

## Service Management

### View Logs

```bash
# All services
docker-compose -f docker-compose.prod.yml logs -f

# Specific service
docker-compose -f docker-compose.prod.yml logs -f api
docker-compose -f docker-compose.prod.yml logs -f web
```

### Check Service Status

```bash
docker-compose -f docker-compose.prod.yml ps
```

### Restart Services

```bash
# All services
docker-compose -f docker-compose.prod.yml restart

# Specific service
docker-compose -f docker-compose.prod.yml restart api
```

### Stop Services

```bash
docker-compose -f docker-compose.prod.yml down
```

### Stop and Remove Data

```bash
# WARNING: This will delete all data
docker-compose -f docker-compose.prod.yml down -v
```

## Updating the Application

### Update API

```bash
# Rebuild and restart API
docker-compose -f docker-compose.prod.yml build api
docker-compose -f docker-compose.prod.yml up -d api

# Run migrations if needed
docker-compose -f docker-compose.prod.yml exec api alembic upgrade head
```

### Update Frontend

```bash
# Rebuild frontend
cd ../web
npm run build
cd ../docker

# Rebuild and restart web service
docker-compose -f docker-compose.prod.yml build web
docker-compose -f docker-compose.prod.yml up -d web
```

## Backup and Restore

### Backup PostgreSQL Database

```bash
docker-compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U openrag openrag > backup.sql
```

### Restore PostgreSQL Database

```bash
docker-compose -f docker-compose.prod.yml exec -T postgres \
  psql -U openrag -d openrag < backup.sql
```

### Backup Volumes

```bash
# Create backup directory
mkdir -p backups

# Backup PostgreSQL data volume
docker run --rm -v openrag_postgres-data:/data -v $(pwd)/backups:/backup \
  alpine tar czf /backup/postgres-data.tar.gz -C /data .

# Backup Milvus data
docker run --rm -v openrag_milvus-data:/data -v $(pwd)/backups:/backup \
  alpine tar czf /backup/milvus-data.tar.gz -C /data .

# Backup uploads
docker run --rm -v openrag_api-uploads:/data -v $(pwd)/backups:/backup \
  alpine tar czf /backup/uploads.tar.gz -C /data .
```

## Monitoring

### Health Checks

All services have health checks configured. Check status:

```bash
docker-compose -f docker-compose.prod.yml ps
```

Healthy services show "healthy" in the status column.

### Resource Usage

```bash
docker stats
```

## Troubleshooting

### Services Won't Start

1. Check logs:
   ```bash
   docker-compose -f docker-compose.prod.yml logs
   ```

2. Verify environment variables:
   ```bash
   cat .env
   ```

3. Check disk space:
   ```bash
   df -h
   ```

### API Can't Connect to Database

1. Wait for PostgreSQL to be healthy:
   ```bash
   docker-compose -f docker-compose.prod.yml ps postgres
   ```

2. Check PostgreSQL logs:
   ```bash
   docker-compose -f docker-compose.prod.yml logs postgres
   ```

3. Verify database credentials in `.env`

### Frontend Shows 502 Error

1. Check if API is running:
   ```bash
   docker-compose -f docker-compose.prod.yml ps api
   ```

2. Test API directly:
   ```bash
   curl http://localhost:8001/health
   ```

3. Check Nginx logs:
   ```bash
   docker-compose -f docker-compose.prod.yml logs web
   ```

### Milvus Connection Issues

Milvus takes 60-90 seconds to start. Wait and check:

```bash
docker-compose -f docker-compose.prod.yml logs milvus
curl http://localhost:9091/healthz
```

## A02 中文全文索引迁移与回滚

迁移以单个 workspace 为最小单位。执行期间必须暂停该 workspace 的文件处理和删除；默认配置保持
`ELASTICSEARCH__CHUNK_INDEX_MODE=legacy`，审计全部通过前不得切换。

在 `openrag` 目录依次执行：

```powershell
python scripts/reindex_es_content_exact_v2.py --workspace-id <ID> --dry-run `
  --output ../docs/a02-migration-local/workspace-<ID>-dry-run.json
python scripts/reindex_es_content_exact_v2.py --workspace-id <ID> --apply `
  --output ../docs/a02-migration-local/workspace-<ID>-apply.json
python scripts/audit_es_chunk_consistency.py --workspace-id <ID> --index-version v2 `
  --output ../docs/a02-migration-local/workspace-<ID>-v2-audit.json
python scripts/reindex_es_content_exact_v2.py --workspace-id <ID> --switch-aliases `
  --output ../docs/a02-migration-local/workspace-<ID>-alias-state.json
python scripts/audit_es_chunk_consistency.py --workspace-id <ID> --index-version alias `
  --output ../docs/a02-migration-local/workspace-<ID>-alias-audit.json
```

任一报告出现 L2 缺失、DB/ES 数量或归属不一致、Mapping/Schema/Hash 不一致、必填字段缺失、
未知字段、token 字段或部分 bulk 失败时必须停止。切换成功并完成 smoke query 后，API 与 Worker
同时设置：

```text
ELASTICSEARCH__HYBRID_RECALL_MODE=independent_rrf
ELASTICSEARCH__CHUNK_INDEX_MODE=v2_alias
```

回滚必须使用切换时保存的同一份 alias 状态；命令会在恢复后执行 legacy smoke query，失败时恢复
回滚前状态：

```powershell
python scripts/reindex_es_content_exact_v2.py --workspace-id <ID> `
  --restore-aliases-from ../docs/a02-migration-local/workspace-<ID>-alias-state.json `
  --output ../docs/a02-migration-local/workspace-<ID>-rollback.json
```

随后将 API 与 Worker 的 `ELASTICSEARCH__CHUNK_INDEX_MODE` 恢复为 `legacy`。迁移和回滚流程均不
删除 v1 索引，也不重新切块。

## Production Recommendations

### Security

1. Change all default passwords in `.env`
2. Use strong SECRET_KEY (32+ random characters)
3. Configure firewall to restrict port access
4. Use HTTPS with reverse proxy (nginx/traefik)
5. Regularly update Docker images

### Performance

1. Allocate sufficient resources:
   - API: 1-2 CPU, 2GB RAM
   - PostgreSQL: 1-2 CPU, 2GB RAM
   - Milvus: 2-4 CPU, 4GB RAM

2. Configure PostgreSQL for production (managed RDS recommended):
   - Tune `shared_buffers`, `work_mem`, autovacuum
   - Enable slow query / `log_min_duration_statement` as needed

### Scaling

1. 调整文档解析吞吐：修改 `task-worker` 的环境变量（如 `WORKER_NUM`、轮询间隔），或拆分多套 Worker 部署。当前 `docker-compose.prod.yml` 为 `task-worker` 固定了 `container_name`，不能使用 `docker compose up --scale task-worker=...` 多实例扩同一服务名。

2. Use external managed services:
   - AWS RDS for PostgreSQL (or Aurora PostgreSQL)
   - Zilliz Cloud for Milvus

3. Deploy API behind load balancer

## File Structure

```
docker/
├── docker-compose.prod.yml    # Production compose file
├── docker-compose.dev.yml     # Local infra only
├── docker-compose.worker.yml  # Standalone worker (e.g. API on host)
├── Dockerfile.api             # API container image
├── Dockerfile.worker          # Task worker image
├── Dockerfile.web             # Frontend container image
├── .env.example               # Environment template
├── nginx/
│   └── nginx.conf             # Nginx: `/api/` → `http://api:8000/`
└── README.md                  # This file
```

## Support

For issues and questions:
- Check logs first
- Review troubleshooting section
- 参阅仓库根目录 `README.md`；方案与技术见 `docs/项目方案与技术概述.md`，运维与 API 见 `docs/01`～`04`
- Open GitHub issue with logs and configuration
