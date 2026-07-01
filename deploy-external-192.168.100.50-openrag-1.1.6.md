# OpenRag 1.1.6 外网服务器 Docker 首次部署执行指南

目标服务器：`192.168.100.50`

目标系统：Ubuntu 24.04.3 LTS amd64

部署版本：`1.1.6`

本地资源目录：`E:\project\OpenRag\artifacts\1.1.6`

服务器部署根目录：`/home/guozhi/Documents/openrag`

服务器资源目录：`/home/guozhi/Documents/openrag/artifacts`

## 0. 结论和前提

可以直接利用本地现有资源做这台 Ubuntu 24.04 服务器的 Docker 离线部署。

已核对到的可用资源如下：

```text
E:\project\OpenRag\artifacts\1.1.6\openrag-1.1.6-manifest.json
E:\project\OpenRag\artifacts\1.1.6\openrag-1.1.6-src.zip
E:\project\OpenRag\artifacts\1.1.6\openrag-1.1.6-src.zip.sha256
E:\project\OpenRag\artifacts\1.1.6\openrag-app-images-1.1.6.tar
E:\project\OpenRag\artifacts\1.1.6\openrag-app-images-1.1.6.tar.sha256
E:\project\OpenRag\artifacts\1.1.6\openrag-third-party-images.tar
E:\project\OpenRag\artifacts\1.1.6\openrag-third-party-images.tar.sha256
E:\project\OpenRag\artifacts\offline-docker-ubuntu2404-amd64.zip
E:\project\OpenRag\docker\.env
```

不要混用 `E:\project\OpenRag\artifacts` 根目录下的 `1.1.4` 产物。那套是旧版本。

本指南假设：

1. 你可以从本地 Windows 机器 SSH 到 `guozhi@192.168.100.50`。
2. 服务器 CPU 架构是 `x86_64` 或 `amd64`。
3. 服务器可能不能稳定访问公网，所以服务器端不执行 `docker pull`、`docker compose build`、`npm install`、`pip install`。
4. 对外只开放 Web 端口 `80`。API 直连端口使用 `18001`，只绑定到 `127.0.0.1`，外部浏览器通过 Web 的 `/api` 反向代理访问后端。
5. `docker/.env` 内的 `OPENAI_BASE_URL` 必须是服务器可访问的模型网关地址。
6. 已确认服务器基础配置：`x86_64`，`16 CPU`，内存 `31Gi`，根盘 `/dev/vda2` 为 `492G`，可用约 `456G`。

## 1. 本地预检查

在 Windows PowerShell 执行：

```powershell
cd E:\project\OpenRag

$Version = "1.1.6"
$ArtifactDir = "E:\project\OpenRag\artifacts\1.1.6"

Get-ChildItem $ArtifactDir | Select-Object Length, Name
Get-Item E:\project\OpenRag\artifacts\offline-docker-ubuntu2404-amd64.zip
Test-Path E:\project\OpenRag\docker\.env
```

预期：

```text
openrag-1.1.6-src.zip
openrag-1.1.6-src.zip.sha256
openrag-1.1.6-manifest.json
openrag-app-images-1.1.6.tar
openrag-app-images-1.1.6.tar.sha256
openrag-third-party-images.tar
openrag-third-party-images.tar.sha256
offline-docker-ubuntu2404-amd64.zip 存在
docker\.env 存在
```

校验本地 SHA256：

```powershell
$checks = @(
  "openrag-1.1.6-src.zip",
  "openrag-app-images-1.1.6.tar",
  "openrag-third-party-images.tar"
)

foreach ($file in $checks) {
  $actual = (Get-FileHash "$ArtifactDir\$file" -Algorithm SHA256).Hash.ToLower()
  $expected = (Get-Content "$ArtifactDir\$file.sha256").Split("  ")[0].Trim().ToLower()
  if ($actual -ne $expected) {
    throw "SHA256 mismatch: $file"
  }
  Write-Host "OK $file"
}
```

我本地已核对到的哈希如下：

```text
521f41b711334639b600f87def0d0e07cfbfed1919a74b5fa432b25ce557e614  openrag-1.1.6-src.zip
77bb0ea6b9c7a5040def12df5df81b00a9463be3643591d2ce3f66fed2cf95f4  openrag-app-images-1.1.6.tar
a31112fb7303437a94d0a0ecebb33d4f0580cddee758fabae007f6ac66678b51  openrag-third-party-images.tar
```

## 2. 本地设置上传变量

已确认服务器 SSH 用户为 `guozhi`。

```powershell
$Server = "192.168.100.50"
$User = "guozhi"
$Target = "$($User)@$($Server)"
$RemoteHome = "/home/guozhi/Documents/openrag"
$Version = "1.1.6"
$ArtifactDir = "E:\project\OpenRag\artifacts\1.1.6"
```

如果需要堡垒机，后续 `ssh` 和 `scp` 命令加上：

```powershell
-o ProxyJump=<堡垒机用户>@<堡垒机地址>
```

## 3. 服务器基础确认

先登录服务器：

```powershell
ssh $Target
```

在服务器执行：

```bash
hostnamectl
uname -m
lscpu | grep -E 'Architecture|Model name|CPU\(s\)|Thread|Core|Socket'
free -h
df -h /
ip addr
```

预期：

```text
Operating System: Ubuntu 24.04...
uname -m: x86_64
CPU: 16
Mem: 31Gi
Disk: /dev/vda2 492G，总可用约 456G
```

如果 `uname -m` 不是 `x86_64`，先停止部署。当前离线 Docker 包和镜像包是 amd64 资源。

## 4. 创建部署目录

在服务器执行：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag

sudo mkdir -p "$SERVER_HOME/artifacts" "$SERVER_HOME/releases" "$SERVER_HOME/shared" "$SERVER_HOME/backups"
sudo chown -R "$USER:$USER" "$SERVER_HOME"
chmod 755 "$SERVER_HOME" "$SERVER_HOME/artifacts" "$SERVER_HOME/releases" "$SERVER_HOME/shared" "$SERVER_HOME/backups"
```

验证：

```bash
ls -ld /home/guozhi/Documents/openrag /home/guozhi/Documents/openrag/artifacts /home/guozhi/Documents/openrag/releases /home/guozhi/Documents/openrag/shared /home/guozhi/Documents/openrag/backups
```

预期：目录存在，当前用户有写权限。

## 5. 确认离线文件

你已经把离线资源保存到了服务器：

```text
/home/guozhi/Documents/openrag/artifacts/
```

如果该目录下已经有截图里的 7 个资源文件，可以跳过 artifacts 大文件上传，只需要确保运行配置文件存在。

如果 `.env` 已经被复制到了 `artifacts/.env`，在服务器执行：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
mkdir -p "$SERVER_HOME/shared"
cp "$SERVER_HOME/artifacts/.env" "$SERVER_HOME/shared/openrag.env"
chmod 600 "$SERVER_HOME/shared/openrag.env"
sed -i 's/\r$//' "$SERVER_HOME/shared/openrag.env"
test -s "$SERVER_HOME/shared/openrag.env"
```

如果服务器上还没有 `.env`，从本地 Windows PowerShell 上传：

```powershell
scp E:\project\OpenRag\docker\.env "$($Target):$RemoteHome/shared/openrag.env"
```

如果后续需要重新上传 artifacts，才回到本地 Windows PowerShell 执行：

```powershell
scp E:\project\OpenRag\artifacts\offline-docker-ubuntu2404-amd64.zip "$($Target):$RemoteHome/artifacts/"

scp "$ArtifactDir\openrag-$Version-src.zip" "$($Target):$RemoteHome/artifacts/"
scp "$ArtifactDir\openrag-$Version-src.zip.sha256" "$($Target):$RemoteHome/artifacts/"
scp "$ArtifactDir\openrag-$Version-manifest.json" "$($Target):$RemoteHome/artifacts/"
scp "$ArtifactDir\openrag-app-images-$Version.tar" "$($Target):$RemoteHome/artifacts/"
scp "$ArtifactDir\openrag-app-images-$Version.tar.sha256" "$($Target):$RemoteHome/artifacts/"
scp "$ArtifactDir\openrag-third-party-images.tar" "$($Target):$RemoteHome/artifacts/"
scp "$ArtifactDir\openrag-third-party-images.tar.sha256" "$($Target):$RemoteHome/artifacts/"
```

注意：`openrag.env` 里有真实密钥，不要贴到聊天或公共文档里。

服务器验证：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
export DEPLOY_VERSION=1.1.6

ls -lh "$SERVER_HOME/artifacts"
test -s "$SERVER_HOME/artifacts/offline-docker-ubuntu2404-amd64.zip"
test -s "$SERVER_HOME/artifacts/openrag-$DEPLOY_VERSION-src.zip"
test -s "$SERVER_HOME/artifacts/openrag-$DEPLOY_VERSION-src.zip.sha256"
test -s "$SERVER_HOME/artifacts/openrag-$DEPLOY_VERSION-manifest.json"
test -s "$SERVER_HOME/artifacts/openrag-app-images-$DEPLOY_VERSION.tar"
test -s "$SERVER_HOME/artifacts/openrag-app-images-$DEPLOY_VERSION.tar.sha256"
test -s "$SERVER_HOME/artifacts/openrag-third-party-images.tar"
test -s "$SERVER_HOME/artifacts/openrag-third-party-images.tar.sha256"
test -s "$SERVER_HOME/shared/openrag.env"
```

预期：所有 `test -s` 都没有输出错误。

## 6. 安装 Docker 和 Compose

如果服务器已经有可用 Docker，可以先验证：

```bash
docker --version
docker compose version
docker info
```

如果这些命令可用，可以跳过本节安装步骤。

如果未安装，执行离线安装：

```bash
cd /home/guozhi/Documents/openrag/artifacts

if command -v unzip >/dev/null 2>&1; then
  unzip -q offline-docker-ubuntu2404-amd64.zip -d /home/guozhi/Documents/openrag/docker-offline
elif command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e offline-docker-ubuntu2404-amd64.zip /home/guozhi/Documents/openrag/docker-offline
else
  echo "Neither unzip nor python3 is available; cannot extract offline Docker package." >&2
  exit 1
fi

cd /home/guozhi/Documents/openrag/docker-offline

sudo dpkg -i ./*.deb

sudo systemctl enable --now docker
docker --version
docker compose version
```

如果 `dpkg -i` 报依赖缺失，不要用 `--force-depends` 硬装，说明离线包不完整，需要回本地补包。

把当前用户加入 docker 组：

```bash
sudo usermod -aG docker "$USER"
newgrp docker
docker info
```

预期：`docker info` 正常输出，不报 permission denied。

## 7. 配置系统参数

Elasticsearch 需要提高 `vm.max_map_count`：

```bash
sudo sysctl -w vm.max_map_count=262144
grep -q '^vm.max_map_count=' /etc/sysctl.conf \
  && sudo sed -i 's/^vm.max_map_count=.*/vm.max_map_count=262144/' /etc/sysctl.conf \
  || echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf
```

验证：

```bash
sysctl vm.max_map_count
```

预期：

```text
vm.max_map_count = 262144
```

检查 Docker 数据目录空间：

```bash
docker info | grep -i 'Docker Root Dir'
df -h /var/lib/docker /home/guozhi/Documents/openrag 2>/dev/null || df -h /
```

预期：Docker Root Dir 所在分区空间足够。首次导入镜像和运行容器建议至少预留 30GB；当前服务器根盘约 `492G`、可用约 `456G`，空间足够。

## 8. 校验服务器端 SHA256

在服务器执行：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
export DEPLOY_VERSION=1.1.6
cd "$SERVER_HOME/artifacts"

sha256sum -c "openrag-$DEPLOY_VERSION-src.zip.sha256"
sha256sum -c "openrag-app-images-$DEPLOY_VERSION.tar.sha256"
sha256sum -c "openrag-third-party-images.tar.sha256"
```

预期：

```text
openrag-1.1.6-src.zip: OK
openrag-app-images-1.1.6.tar: OK
openrag-third-party-images.tar: OK
```

## 9. 检查运行配置

服务器执行：

```bash
export ENV_FILE=/home/guozhi/Documents/openrag/shared/openrag.env
chmod 600 "$ENV_FILE"
sed -i 's/\r$//' "$ENV_FILE"

for key in POSTGRES_PASSWORD SECRET_KEY MINIO_ROOT_USER MINIO_ROOT_PASSWORD OPENAI_API_KEY OPENAI_BASE_URL EMBEDDING_MODEL EMBEDDING_DIMENSION WEB_PORT ELASTICSEARCH__ENABLED ELASTICSEARCH__HOSTS; do
  if ! grep -q "^${key}=" "$ENV_FILE"; then
    echo "$key=MISSING"
  elif [ -z "$(grep "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)" ]; then
    echo "$key=EMPTY"
  else
    echo "$key=SET"
  fi
done
```

预期：关键项都是 `SET`。不要直接 `cat openrag.env`，避免泄漏密钥。

确认模型网关可访问：

```bash
OPENAI_BASE_URL="$(grep '^OPENAI_BASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
echo "$OPENAI_BASE_URL"
curl -I --max-time 10 "$OPENAI_BASE_URL" || true
```

这里返回 `200`、`401`、`404` 都不算部署阻塞，重点是服务器能连到该地址。当前环境里 `http://litellm.guozhijishu.com/v1` 返回 `HTTP/1.1 404 Not Found` 是可接受结果，因为很多 OpenAI 兼容网关不会在 `/v1` 根路径提供页面；只要不是连接超时、DNS 失败、connection refused，就可以继续下一步。如果超时或 DNS 失败，后续 embedding、检索可能失败。

## 10. 解压源码包并切换 current

服务器执行：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
export DEPLOY_VERSION=1.1.6
export ARTIFACT_DIR="$SERVER_HOME/artifacts"
export RELEASE_DIR="$SERVER_HOME/releases/openrag-$DEPLOY_VERSION"

rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

if command -v unzip >/dev/null 2>&1; then
  unzip -q "$ARTIFACT_DIR/openrag-$DEPLOY_VERSION-src.zip" -d "$RELEASE_DIR"
elif command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e "$ARTIFACT_DIR/openrag-$DEPLOY_VERSION-src.zip" "$RELEASE_DIR"
else
  echo "Neither unzip nor python3 is available; cannot extract source package." >&2
  exit 1
fi

ln -sfn "$RELEASE_DIR" "$SERVER_HOME/current"

test -s "$SERVER_HOME/current/docker/docker-compose.prod.yml"
test -s "$SERVER_HOME/current/docker/Dockerfile.api"
test -s "$SERVER_HOME/current/docker/Dockerfile.web"
test -s "$SERVER_HOME/current/docker/Dockerfile.worker"
```

预期：所有 `test -s` 都没有报错。

## 11. 导入 Docker 镜像

服务器执行：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
export DEPLOY_VERSION=1.1.6
export ARTIFACT_DIR="$SERVER_HOME/artifacts"

docker load -i "$ARTIFACT_DIR/openrag-third-party-images.tar"
docker load -i "$ARTIFACT_DIR/openrag-app-images-$DEPLOY_VERSION.tar"
```

验证镜像：

```bash
docker image inspect openrag-api:latest >/dev/null
docker image inspect openrag-api:1.1.6 >/dev/null
docker image inspect openrag-web:latest >/dev/null
docker image inspect openrag-task-worker:latest >/dev/null
docker image inspect postgres:16-alpine >/dev/null
docker image inspect quay.io/coreos/etcd:v3.5.5 >/dev/null
docker image inspect minio/minio:RELEASE.2023-03-20T20-16-18Z >/dev/null
docker image inspect milvusdb/milvus:v2.4.17 >/dev/null
docker image inspect docker.elastic.co/elasticsearch/elasticsearch:8.12.2 >/dev/null
```

预期：全部无输出错误。

## 12. 创建服务器专用覆盖配置

服务器执行：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
export SHARED_DIR="$SERVER_HOME/shared"
export ENV_FILE="$SHARED_DIR/openrag.env"
export API_PORT=18001
export WEB_PORT=80

mkdir -p "$SHARED_DIR"
if [ ! -s "$ENV_FILE" ] && [ -s "$SERVER_HOME/artifacts/.env" ]; then
  cp "$SERVER_HOME/artifacts/.env" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
sed -i 's/\r$//' "$ENV_FILE"
test -s "$ENV_FILE"

ss -ltn | grep -E '(:80|:18001)' || true
```

预期：如果 `80` 已被其他服务占用，需要先释放 80，或者把 `WEB_PORT` 改成其他端口，例如 `8080`，并使用 `http://192.168.100.50:8080/` 访问。`18001` 如果被占用，可以换成其他本机端口。

继续写入端口变量：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
export SHARED_DIR="$SERVER_HOME/shared"
export ENV_FILE="$SHARED_DIR/openrag.env"
export API_PORT=18001
export WEB_PORT=80

mkdir -p "$SHARED_DIR"
if [ ! -s "$ENV_FILE" ] && [ -s "$SERVER_HOME/artifacts/.env" ]; then
  cp "$SERVER_HOME/artifacts/.env" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
sed -i 's/\r$//' "$ENV_FILE"
test -s "$ENV_FILE"

grep -q '^API_PORT=' "$ENV_FILE" \
  && sed -i "s|^API_PORT=.*|API_PORT=$API_PORT|" "$ENV_FILE" \
  || echo "API_PORT=$API_PORT" >> "$ENV_FILE"

grep -q '^WEB_PORT=' "$ENV_FILE" \
  && sed -i "s|^WEB_PORT=.*|WEB_PORT=$WEB_PORT|" "$ENV_FILE" \
  || echo "WEB_PORT=$WEB_PORT" >> "$ENV_FILE"

cat > "$SHARED_DIR/app-config.js" <<'EOF'
window.__OPENRAG_CONFIG__ = {
  apiBaseUrl: "/api"
};
EOF
```

创建 `docker-compose.server.yml`：

```bash
cat > "$SHARED_DIR/docker-compose.server.yml" <<'YAML'
services:
  api:
    image: openrag-api:latest
    pull_policy: never
    ports: !override
      - "127.0.0.1:${API_PORT:-18001}:8000"
    environment:
      - STORAGE_ENDPOINT=milvus-minio:9000
      - STORAGE_ACCESS_KEY=${MINIO_ROOT_USER:-minioadmin}
      - STORAGE_SECRET_KEY=${MINIO_ROOT_PASSWORD:-minioadmin}
      - HIERARCHY_STORAGE_PATH=/app/storage/hierarchies
    volumes:
      - api-uploads:/app/uploads
      - hierarchy-data:/app/storage/hierarchies

  web:
    image: openrag-web:latest
    pull_policy: never
    ports: !override
      - "${WEB_PORT:-80}:80"
    volumes:
      - /home/guozhi/Documents/openrag/shared/app-config.js:/usr/share/nginx/html/app-config.js:ro

  task-worker:
    image: openrag-task-worker:latest
    pull_policy: never
    environment:
      - STORAGE_ENDPOINT=milvus-minio:9000
      - STORAGE_ACCESS_KEY=${MINIO_ROOT_USER:-minioadmin}
      - STORAGE_SECRET_KEY=${MINIO_ROOT_PASSWORD:-minioadmin}
      - HIERARCHY_STORAGE_PATH=/app/storage/hierarchies
    volumes:
      - hierarchy-data:/app/storage/hierarchies

  postgres:
    pull_policy: never
    ports: !override
      - "127.0.0.1:5432:5432"

  milvus-etcd:
    pull_policy: never

  milvus-minio:
    pull_policy: never
    ports: !override
      - "127.0.0.1:9001:9001"
      - "127.0.0.1:9000:9000"

  milvus:
    pull_policy: never
    ports: !override
      - "127.0.0.1:19530:19530"
      - "127.0.0.1:9091:9091"
    environment:
      MINIO_ACCESS_KEY_ID: ${MINIO_ROOT_USER:-minioadmin}
      MINIO_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD:-minioadmin}

  elasticsearch:
    pull_policy: never
    ports: !override
      - "127.0.0.1:${ELASTICSEARCH_PORT:-9200}:9200"

  trace-dashboard:
    profiles:
      - trace-dashboard
    ports: !override
      - "127.0.0.1:${TRACE_DASHBOARD_PORT:-8501}:8501"

volumes:
  hierarchy-data:
    driver: local
YAML
```

## 13. 创建 Compose 辅助命令

服务器执行：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
export SHARED_DIR="$SERVER_HOME/shared"

cat > "$SHARED_DIR/dc-openrag" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SERVER_HOME="/home/guozhi/Documents/openrag"
SHARED_DIR="$SERVER_HOME/shared"
CURRENT_LINK="$SERVER_HOME/current"
exec docker compose -p openrag \
  --env-file "$SHARED_DIR/openrag.env" \
  -f "$CURRENT_LINK/docker/docker-compose.prod.yml" \
  -f "$SHARED_DIR/docker-compose.server.yml" \
  "$@"
EOF
chmod +x "$SHARED_DIR/dc-openrag"

cat > "$SHARED_DIR/migrate-openrag" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SERVER_HOME="/home/guozhi/Documents/openrag"
SHARED_DIR="$SERVER_HOME/shared"
CURRENT_LINK="$SERVER_HOME/current"
exec docker compose -p openrag \
  --env-file "$SHARED_DIR/openrag.env" \
  -f "$CURRENT_LINK/docker/docker-compose.prod.yml" \
  -f "$SHARED_DIR/docker-compose.server.yml" \
  run --rm --no-deps api python -m alembic upgrade head
EOF
chmod +x "$SHARED_DIR/migrate-openrag"
```

验证：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag config --quiet
/home/guozhi/Documents/openrag/shared/dc-openrag config --services
```

预期：无报错，并能看到 `api`、`web`、`postgres`、`milvus`、`elasticsearch`、`task-worker` 等服务。

如果这里报 `!override` 不支持，先确认你用的是离线包里安装的新版 Docker Compose：

```bash
docker compose version
```

不要继续启动。先升级 Compose 或重新安装本指南第 6 步的离线 Docker 包。

## 14. 首次初始化数据库并标记迁移版本

服务器执行：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag up -d --no-build postgres
/home/guozhi/Documents/openrag/shared/dc-openrag ps postgres

for i in $(seq 1 60); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' openrag-postgres-prod 2>/dev/null || true)"
  echo "postgres status: ${status:-missing}"
  if [ "$status" = "healthy" ] || [ "$status" = "running" ]; then
    docker exec openrag-postgres-prod pg_isready -U openrag -d openrag && break
  fi
  sleep 2
done

docker exec openrag-postgres-prod pg_isready -U openrag -d openrag

docker exec openrag-postgres-prod psql -U openrag -d openrag -tAc "select to_regclass('public.files'), to_regclass('public.workspaces'), to_regclass('public.document_parse_artifacts'), to_regclass('public.alembic_version');"

/home/guozhi/Documents/openrag/shared/dc-openrag run --rm --no-deps api python -c "import openrag.models; from openrag.database import init_db; init_db(); print('init_db ok')"
/home/guozhi/Documents/openrag/shared/dc-openrag run --rm --no-deps api python -m alembic stamp head

docker exec openrag-postgres-prod psql -U openrag -d openrag -tAc "select to_regclass('public.files'), to_regclass('public.workspaces'), to_regclass('public.document_parse_artifacts'), to_regclass('public.alembic_version');"
/home/guozhi/Documents/openrag/shared/dc-openrag run --rm --no-deps api python -m alembic current
```

预期：

```text
postgres running 或 healthy
pg_isready 返回 accepting connections
第一次查询里 files/workspaces 可能为空
init_db ok
alembic stamp head 执行成功
第二次查询里 files/workspaces/document_parse_artifacts/alembic_version 都非空
alembic current 显示 head 版本
```

如果 `pg_isready` 未返回 `accepting connections`，不要执行迁移，先查看数据库日志：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag ps postgres
/home/guozhi/Documents/openrag/shared/dc-openrag logs --tail=200 postgres
```

说明：当前 OpenRag 首次部署不是纯 Alembic 建库。API 启动逻辑会通过 SQLAlchemy `Base.metadata.create_all()` 创建基础表和当前模型表，而 Alembic 迁移 `20260527_0001_trace_eval_tables` 依赖已存在的 `files`、`workspaces`、`users` 表。全新数据库如果直接执行 `alembic upgrade head`，会因为 `files` 表不存在而失败。因此首次部署应先执行 `init_db()` 创建当前全量 schema，再用 `alembic stamp head` 标记迁移版本。后续已有库升级时再使用 `/home/guozhi/Documents/openrag/shared/migrate-openrag`。

## 15. 启动全量服务

服务器执行：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag up -d --no-build
```

启动后查看：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag ps
```

预期：核心容器处于 `running` 或 `healthy`。

如果某个容器异常：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag logs --tail=200 <service-name>
```

常见服务名：

```text
api
web
postgres
milvus
milvus-minio
milvus-etcd
elasticsearch
task-worker
```

## 16. 服务器本机验证

服务器执行：

```bash
curl -i http://127.0.0.1/health
curl -i http://127.0.0.1/api/health
curl -i http://127.0.0.1:18001/health
curl -i http://127.0.0.1/app-config.js
```

预期：

```text
/health 返回 200
/api/health 返回 200
127.0.0.1:18001/health 返回 200
/app-config.js 返回 200，内容包含 apiBaseUrl: "/api"
```

检查关键端口绑定：

```bash
sudo ss -ltnp | grep -E ':80|:18001|:5432|:9000|:9001|:9200|:19530|:9091'
```

预期：

```text
0.0.0.0:80 或 :::80 对外开放
127.0.0.1:18001 只本机可访问
127.0.0.1:5432 只本机可访问
127.0.0.1:9000/9001 只本机可访问
127.0.0.1:9200 只本机可访问
127.0.0.1:19530/9091 只本机可访问
```

## 17. 外部访问验证

在本地 Windows PowerShell 执行：

```powershell
curl.exe -I http://192.168.100.50/
curl.exe -I http://192.168.100.50/app-config.js
curl.exe -I http://192.168.100.50/api/health
```

预期：

```text
HTTP/1.1 200 OK
```

浏览器打开：

```text
http://192.168.100.50/
```

登录页能打开后，在浏览器开发者工具 Network 里确认登录请求地址是：

```text
http://192.168.100.50/api/users/login
```

不应该是：

```text
http://localhost:8001/users/login
```

如果登录返回 `401`，说明前端到后端链路已经通了，问题在账号、邮箱、激活状态或密码，不是部署网络问题。

## 18. 防火墙检查

如果服务器开启了 `ufw`：

```bash
sudo ufw status
sudo ufw allow 80/tcp
sudo ufw reload
```

如果还有云安全组、防火墙或网关策略，也只需要放行：

```text
TCP 80
```

除非你明确需要远程调试，否则不要对外开放 `18001`、`5432`、`9000`、`9001`、`9200`、`19530`、`9091`。

## 19. 注册或检查用户

如果首次部署没有用户，可以浏览器访问：

```text
http://192.168.100.50/register
```

如果需要直接检查用户表：

```bash
docker exec openrag-postgres-prod psql -U openrag -d openrag -c "select id, username, email, is_active, is_admin from users;"
```

## 20. 完成标准

满足以下条件才算部署完成：

1. `/home/guozhi/Documents/openrag/shared/dc-openrag ps` 中核心容器为 `running` 或 `healthy`。
2. `curl -i http://127.0.0.1/health` 返回 `200`。
3. `curl -i http://127.0.0.1/api/health` 返回 `200`。
4. `curl -i http://127.0.0.1:18001/health` 返回 `200`。
5. `curl -i http://127.0.0.1/app-config.js` 返回 `200`，内容包含 `apiBaseUrl: "/api"`。
6. 本地浏览器可以打开 `http://192.168.100.50/`。
7. 登录请求发往 `/api/users/login`。
8. `sudo ss -ltnp` 显示只有 Web 端口 `80` 对外开放，数据库、MinIO、Milvus、Elasticsearch、API 直连端口都只绑定 `127.0.0.1`。

## 21. 回滚和重启

只重启服务：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag restart
```

停止服务：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag down
```

查看日志：

```bash
/home/guozhi/Documents/openrag/shared/dc-openrag logs --tail=200 api
/home/guozhi/Documents/openrag/shared/dc-openrag logs --tail=200 web
/home/guozhi/Documents/openrag/shared/dc-openrag logs --tail=200 task-worker
```

首次部署不要执行会删除数据卷的命令，例如：

```text
docker compose down -v
docker volume rm ...
```

除非你明确确认要清空数据库、Milvus、MinIO、Elasticsearch 数据。

