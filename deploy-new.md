# OpenRag 内网新服务器离线部署准备说明

## 1. 部署口径

本次部署基于以下前提：

- 目标服务器直接部署 OpenRag，不加入现有 K8s 集群。
- 目标服务器系统为 Ubuntu 24.04.3 LTS。
- 目标服务器不可联网。
- 服务器资源已知：内存约 46GB，磁盘约 800GB。
- 代码基准使用本地 `deploy-main` 分支。
- 部署方式使用 Docker Compose 离线单机部署。
- `trace-dashboard` 是可选 profile 服务，默认不构建、不打包、不部署。

已确认信息：

- CPU 架构：`x86_64/amd64` ✓
- CPU 核数：32 核（NUMA node0，CPU 0-31）。
- 服务器 IP：`172.18.64.16`，登录用户：`root`，主机名：`ubuntu24-node`。
- 系统：Ubuntu 24.04.3 LTS（Codename: noble），KVM 虚拟机。
- 磁盘：800 GB（`/dev/sda`）整盘通过 LVM 挂载为根目录 `/`，可用 738 GB。**Docker 数据目录无需迁移，`/var/lib/docker` 直接在根分区。**

待确认信息：

- 堡垒机是否支持 `scp` / `sftp` 上传大文件。
- 服务器是否能访问内网模型网关 `OPENAI_BASE_URL`。

执行前硬门槛：

- 必须先确认自己已经登录到目标 Ubuntu 24.04.3 LTS 服务器，而不是堡垒机或普通跳板机。
- 如果 `hostnamectl` 显示的不是 Ubuntu 24.04.3 LTS，或者 `uname -m` 显示的是未评估过的架构，先停止部署。
- 不要在堡垒机上安装 Docker、导入镜像或部署 OpenRag。

在目标服务器上执行：

```bash
hostnamectl
uname -m
lscpu | grep -E 'Architecture|Model name|CPU\(s\)|Thread|Core|Socket'
free -h
df -h
ip addr
```

预期：

```text
Operating System: Ubuntu 24.04.3 LTS ...
Architecture: x86_64 或 aarch64
CPU(s): 32
```

如果当前机器类似 `Kylin Linux`、`localhost.localdomain`、`172.16.31.90`，先按堡垒机/跳板机处理，不要把它当成 OpenRag 目标服务器。

## 2. 堡垒机登录方式

堡垒机是登录目标服务器前的统一入口。常见链路如下：

```text
本地电脑 -> 堡垒机 -> 目标 Ubuntu 服务器
```

常见方式一：网页堡垒机。

1. 打开堡垒机网页。
2. 使用公司账号登录，按要求完成 MFA 或审批。
3. 在资产或主机列表中选择目标 Ubuntu 服务器。
4. 点击 SSH / 终端进入目标服务器。

常见方式二：SSH 堡垒机。

```bash
ssh <堡垒机账号>@<堡垒机地址>
```

进入堡垒机后再登录目标服务器：

```bash
ssh <目标服务器用户>@<目标服务器IP>
```

如果堡垒机支持 `ProxyJump`，可以从本地直接跳转：

```bash
ssh -J <堡垒机账号>@<堡垒机地址> <目标服务器用户>@<目标服务器IP>
```

上传发布包时，如果支持 `scp -J`，可以使用：

```powershell
scp -o ProxyJump=<堡垒机账号>@<堡垒机地址> `
  artifacts\openrag-app-images-<version>.tar `
  <目标服务器用户>@<目标服务器IP>:/opt/openrag/artifacts/
```

如果堡垒机不支持 `scp` / `sftp`，需要确认是否支持网页上传，或者是否有内网文件中转机。

## 3. 本地需要构建和打包的镜像

默认部署需要构建 3 个 OpenRag 应用镜像：

| 镜像名 | Dockerfile |
|---|---|
| `openrag-api` | `docker/Dockerfile.api` |
| `openrag-web` | `docker/Dockerfile.web` |
| `openrag-task-worker` | `docker/Dockerfile.worker` |

首次部署还需要准备 5 个第三方镜像：

| 镜像 | 用途 | 大小估计 |
|---|---|---|
| `postgres:16-alpine` | 数据库 | ~80 MB |
| `quay.io/coreos/etcd:v3.5.5` | Milvus 依赖 | ~60 MB |
| `minio/minio:RELEASE.2023-03-20T20-16-18Z` | Milvus 对象存储 | ~300 MB |
| `milvusdb/milvus:v2.4.17` | 向量数据库 | ~2.5 GB |
| `docker.elastic.co/elasticsearch/elasticsearch:8.12.2` | 全文检索 | ~1.5 GB |

预期打包产物：

```text
artifacts/openrag-<version>-src.zip                     约 50 MB
artifacts/openrag-<version>-src.zip.sha256
artifacts/openrag-<version>-manifest.json
artifacts/openrag-app-images-<version>.tar              约 3-5 GB
artifacts/openrag-app-images-<version>.tar.sha256
artifacts/openrag-third-party-images.tar                约 10-15 GB
artifacts/openrag-third-party-images.tar.sha256
```

默认不包含：

```text
trace-dashboard
```

原因：`trace-dashboard` 在 `docker/docker-compose.prod.yml` 中属于 `trace-dashboard` profile。普通 `docker compose up -d` 不会启动它，因此默认发布包不需要它的镜像。

**架构注意：** 镜像包内的 Linux 平台必须和服务器 CPU 架构一致。本文默认按 `linux/amd64` 准备；如果目标服务器是 `aarch64/arm64`，先停止，单独确认 5 个第三方镜像是否都有 arm64 版本，并重新设计构建与打包命令。不要把 amd64 镜像包加载到 arm64 服务器上。

## 4. 本地构建命令

在本地 Windows 构建机执行，工作目录为仓库根目录。

### 4.1 构建前预检查

每次构建前逐项确认：

```powershell
cd E:\project\OpenRag

# 1. Docker Desktop 正常运行
docker info | Select-String "Server Version"
# 预期：显示版本号，无报错

# 2. 确认当前分支为 deploy-main
git rev-parse --abbrev-ref HEAD
# 预期：deploy-main

# 3. docker/.env 存在（内含真实配置）
Test-Path docker\.env
# 预期：True
# 若为 False：Copy-Item docker\.env.example docker\.env，然后填入真实值

# 4. artifacts 所在盘剩余空间 >= 50 GB
(Get-PSDrive E).Free / 1GB
# 预期：> 50

# 5. 外网连通（需拉取第三方镜像）
docker pull hello-world
# 预期：正常拉取
```

### 4.2 提前拉取第三方镜像（推荐）

在执行完整构建前单独拉取，方便提前发现网络问题。每条命令等待 `Pull complete` 后再执行下一条：

```powershell
docker pull postgres:16-alpine
docker pull quay.io/coreos/etcd:v3.5.5
docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z
docker pull milvusdb/milvus:v2.4.17
docker pull docker.elastic.co/elasticsearch/elasticsearch:8.12.2
```

milvus 和 elasticsearch 镜像较大，各需 3-5 分钟。

### 4.3 执行构建打包

设置版本号，版本号不要带 `V` 前缀：

```powershell
$Version = "1.1.6"
```

首次部署连第三方镜像一起打包：

```powershell
.\scripts\build-openrag-compose-release.ps1 -Version $Version -IncludeThirdPartyImages
```

脚本内部按 6 个阶段运行：

| 阶段 | 内容 | 耗时估计 |
|---|---|---|
| `[1/6]` | 校验 docker-compose.prod.yml | < 10s |
| `[2/6]` | 打源码 zip + 构建 api/web/worker 镜像 | 5-15 分钟（首次） |
| `[3/6]` | 验证三个应用镜像存在 | < 5s |
| `[4/6]` | `docker save` 应用镜像 → `.tar` | 2-5 分钟 |
| `[5/6]` | 检查/拉取第三方镜像 → `docker save` → `.tar` | 3-8 分钟（已预拉） |
| `[6/6]` | 打印 artifacts 目录清单 | < 5s |

如果只是后续更新业务代码，且第三方镜像版本没有变化，可以不加 `-IncludeThirdPartyImages`。

### 4.4 构建后校验

```powershell
# 确认 7 个产物文件都存在
Get-ChildItem artifacts | Where-Object Name -match "(1\.1\.6|third-party)" | Select-Object Length, Name

$required = @(
  "artifacts\openrag-1.1.6-src.zip",
  "artifacts\openrag-1.1.6-src.zip.sha256",
  "artifacts\openrag-1.1.6-manifest.json",
  "artifacts\openrag-app-images-1.1.6.tar",
  "artifacts\openrag-app-images-1.1.6.tar.sha256",
  "artifacts\openrag-third-party-images.tar",
  "artifacts\openrag-third-party-images.tar.sha256"
)
foreach ($path in $required) {
  if (!(Test-Path $path)) { throw "Missing artifact: $path" }
}

# 校验应用镜像包 SHA256
$recorded = (Get-Content "artifacts\openrag-app-images-1.1.6.tar.sha256").Split("  ")[0]
$actual    = (Get-FileHash "artifacts\openrag-app-images-1.1.6.tar" -Algorithm SHA256).Hash.ToLower()
if ($recorded -eq $actual) { Write-Host "OK  应用镜像包完整" } else { Write-Host "ERR 哈希不匹配！" }

# 校验第三方镜像包 SHA256
$recorded = (Get-Content "artifacts\openrag-third-party-images.tar.sha256").Split("  ")[0]
$actual    = (Get-FileHash "artifacts\openrag-third-party-images.tar" -Algorithm SHA256).Hash.ToLower()
if ($recorded -eq $actual) { Write-Host "OK  第三方镜像包完整" } else { Write-Host "ERR 哈希不匹配！" }
```

## 5. 服务器需要离线准备的安装包

目标服务器不可联网，因此不能在服务器上拉镜像、构建镜像或下载依赖。需要提前准备 Ubuntu 24.04 对应 CPU 架构的离线安装包。

需要准备的软件包：

```text
# Docker 相关
docker-ce
docker-ce-cli
containerd.io
docker-buildx-plugin
docker-compose-plugin

# 基础工具
curl
unzip
ca-certificates
```

注意：

- 如果服务器是 `x86_64/amd64`，准备 amd64 安装包和镜像。
- 如果服务器是 `aarch64/arm64`，准备 arm64 安装包和镜像。
- 镜像架构必须和服务器 CPU 架构一致。

### 5.1 用 Docker 容器下载离线 deb 包（在本地构建机执行）

此方法使用与目标环境一致的 Ubuntu 24.04 容器，确保包版本和依赖树完全匹配。在本地 Windows 构建机（有网络）执行：

```powershell
# 创建存放 deb 包的目录
New-Item -ItemType Directory -Force artifacts\offline-packages

# 下载 Docker 引擎及基础工具的全部 deb 包（含依赖）
# 服务器为 arm64 时将 --platform linux/amd64 和 arch=amd64 改为 linux/arm64 和 arm64
docker run --rm `
  --platform linux/amd64 `
  -v "${PWD}\artifacts\offline-packages:/packages" `
  ubuntu:24.04 bash -c "
    set -e
    apt-get update -qq
    apt-get install -y --no-install-recommends curl ca-certificates gpg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable' \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    mkdir -p /packages/partial
    apt-get \
      -o Dir::Cache::archives=/packages \
      install -y --download-only --reinstall --no-install-recommends \
      docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin \
      curl unzip ca-certificates
    echo '--- deb 包清单 ---'
    find /packages -maxdepth 1 -name '*.deb' -type f -printf '%f\n' | sort
  "
```

下载完成后验证：

```powershell
# 预期：能看到 docker-ce、docker-ce-cli、containerd.io、docker-compose-plugin 等关键 deb
Get-ChildItem artifacts\offline-packages -Filter *.deb | Select-Object Length, Name

$requiredDebPatterns = @(
  "docker-ce_*_amd64.deb",
  "docker-ce-cli_*_amd64.deb",
  "containerd.io_*_amd64.deb",
  "docker-buildx-plugin_*_amd64.deb",
  "docker-compose-plugin_*_amd64.deb"
)
foreach ($pattern in $requiredDebPatterns) {
  if (!(Get-ChildItem artifacts\offline-packages -Filter $pattern)) {
    throw "Missing required deb: $pattern"
  }
}
```

### 5.2 打包离线 deb 包

```powershell
Compress-Archive -Path artifacts\offline-packages\* `
  -DestinationPath artifacts\offline-docker-ubuntu2404-amd64.zip -Force

# 检查大小（预期约 80-150 MB）
(Get-Item artifacts\offline-docker-ubuntu2404-amd64.zip).Length / 1MB
```

### 5.3 服务器端安装（上传后在服务器执行）

将 `offline-docker-ubuntu2404-amd64.zip` 上传到服务器后：

```bash
# 解压
unzip offline-docker-ubuntu2404-amd64.zip -d ~/docker-offline
cd ~/docker-offline

# 安装全部 deb。不要使用 --force-depends；如果失败，说明离线包不完整，需要回到构建机补包。
sudo dpkg -i ./*.deb

# 验证安装成功
docker --version
docker compose version

# 启动 Docker 并设置开机自启
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
# 注销后重新登录，再执行：
docker info
```

## 6. 服务器需要提前配置的内容

建议部署目录：

```bash
sudo mkdir -p /opt/openrag/artifacts /opt/openrag/releases /opt/openrag/shared /opt/openrag/backups
sudo chmod 755 /opt/openrag /opt/openrag/artifacts /opt/openrag/releases /opt/openrag/shared /opt/openrag/backups
```

Elasticsearch 需要的内核参数：

```bash
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf
```

如果 Docker 默认数据目录 `/var/lib/docker` 不在 800GB 数据盘上，建议在 `docker load` 前配置 Docker 数据目录，例如：

```text
/etc/docker/daemon.json
```

示例：

```bash
# 如果实际大盘不是 /data，请先替换成真实路径。
sudo mkdir -p /etc/docker /data/docker
cat <<'EOF' | sudo tee /etc/docker/daemon.json
{
  "data-root": "/data/docker"
}
EOF
```

修改后重启 Docker：

```bash
# 先确认 /data 所在分区容量足够，再使用 /data/docker。
df -h /data || true
sudo systemctl restart docker
sudo docker info | grep -i 'Docker Root Dir'
```

## 7. 需要上传到服务器的文件

将以下文件上传到：

```text
/opt/openrag/artifacts/
```

文件清单：

```text
openrag-<version>-src.zip
openrag-<version>-src.zip.sha256
openrag-<version>-manifest.json
openrag-app-images-<version>.tar
openrag-app-images-<version>.tar.sha256
openrag-third-party-images.tar
openrag-third-party-images.tar.sha256
```

将本地运行配置上传为：

```text
/opt/openrag/shared/openrag.env
```

来源文件：

```text
docker/.env
```

不要把真实密钥写入本文档或公开渠道。

上传后在服务器上校验文件存在：

```bash
SERVER_HOME=/opt/openrag
DEPLOY_VERSION=1.1.6

ls -lh "$SERVER_HOME/artifacts"
test -s "$SERVER_HOME/artifacts/openrag-$DEPLOY_VERSION-src.zip"
test -s "$SERVER_HOME/artifacts/openrag-$DEPLOY_VERSION-src.zip.sha256"
test -s "$SERVER_HOME/artifacts/openrag-$DEPLOY_VERSION-manifest.json"
test -s "$SERVER_HOME/artifacts/openrag-app-images-$DEPLOY_VERSION.tar"
test -s "$SERVER_HOME/artifacts/openrag-app-images-$DEPLOY_VERSION.tar.sha256"
test -s "$SERVER_HOME/artifacts/openrag-third-party-images.tar"
test -s "$SERVER_HOME/artifacts/openrag-third-party-images.tar.sha256"
test -s "$SERVER_HOME/shared/openrag.env"
```

## 8. openrag.env 必须包含的关键配置

`/opt/openrag/shared/openrag.env` 至少需要包含：

```text
POSTGRES_PASSWORD
SECRET_KEY
MINIO_ROOT_USER
MINIO_ROOT_PASSWORD
OPENAI_API_KEY
OPENAI_BASE_URL
EMBEDDING_MODEL
EMBEDDING_DIMENSION
WEB_PORT=80
ELASTICSEARCH__ENABLED=true
ELASTICSEARCH__HOSTS=http://elasticsearch:9200
ELASTICSEARCH__VERIFY_CERTS=false
```

其中 `OPENAI_BASE_URL` 必须是目标服务器能访问的内网模型网关地址。否则上传解析、embedding 入库和检索链路可能失败。

在服务器上只检查状态，不打印真实密钥：

```bash
ENV_FILE=/opt/openrag/shared/openrag.env
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

## 9. 服务器端部署与启动

服务器不可联网，因此服务器上只允许：

```text
docker load
docker compose up -d --no-build
```

服务器上不要执行：

```text
docker compose build
docker pull
npm install
pip install
apt install
```

除非这些命令已经明确配置为使用内网源，且已被验证可用。

### 9.1 准备变量

```bash
export DEPLOY_VERSION="1.1.6"
export SERVER_HOME="/opt/openrag"
export API_PORT="18001"
export WEB_PORT="80"
export ARTIFACT_DIR="$SERVER_HOME/artifacts"
export RELEASE_DIR="$SERVER_HOME/releases/openrag-$DEPLOY_VERSION"
export SHARED_DIR="$SERVER_HOME/shared"
export ENV_FILE="$SHARED_DIR/openrag.env"
```

### 9.2 校验 sha256

```bash
cd "$ARTIFACT_DIR"
sha256sum -c "openrag-$DEPLOY_VERSION-src.zip.sha256"
sha256sum -c "openrag-app-images-$DEPLOY_VERSION.tar.sha256"
sha256sum -c "openrag-third-party-images.tar.sha256"
```

### 9.3 解压源码包并切换 current

```bash
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

if command -v unzip >/dev/null 2>&1; then
  unzip -q "$ARTIFACT_DIR/openrag-$DEPLOY_VERSION-src.zip" -d "$RELEASE_DIR"
elif command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e "$ARTIFACT_DIR/openrag-$DEPLOY_VERSION-src.zip" "$RELEASE_DIR"
else
  echo "Neither unzip nor python3 is available" >&2
  exit 1
fi

ln -sfn "$RELEASE_DIR" "$SERVER_HOME/current"
test -s "$SERVER_HOME/current/docker/docker-compose.prod.yml"
```

### 9.4 导入镜像

```bash
docker load -i "$ARTIFACT_DIR/openrag-third-party-images.tar"
docker load -i "$ARTIFACT_DIR/openrag-app-images-$DEPLOY_VERSION.tar"

docker image inspect openrag-api >/dev/null
docker image inspect openrag-web >/dev/null
docker image inspect openrag-task-worker >/dev/null
docker image inspect postgres:16-alpine >/dev/null
docker image inspect quay.io/coreos/etcd:v3.5.5 >/dev/null
docker image inspect minio/minio:RELEASE.2023-03-20T20-16-18Z >/dev/null
docker image inspect milvusdb/milvus:v2.4.17 >/dev/null
docker image inspect docker.elastic.co/elasticsearch/elasticsearch:8.12.2 >/dev/null
```

### 9.5 创建服务器专用配置

```bash
chmod 600 "$ENV_FILE"
sed -i 's/\r$//' "$ENV_FILE"

if grep -q '^API_PORT=' "$ENV_FILE"; then
  sed -i "s|^API_PORT=.*|API_PORT=$API_PORT|" "$ENV_FILE"
else
  echo "API_PORT=$API_PORT" >> "$ENV_FILE"
fi

if grep -q '^WEB_PORT=' "$ENV_FILE"; then
  sed -i "s|^WEB_PORT=.*|WEB_PORT=$WEB_PORT|" "$ENV_FILE"
else
  echo "WEB_PORT=$WEB_PORT" >> "$ENV_FILE"
fi

cat > "$SHARED_DIR/app-config.js" <<'EOF'
window.__OPENRAG_CONFIG__ = {
  apiBaseUrl: "/api"
};
EOF

cat > "$SHARED_DIR/docker-compose.server.yml" <<YAML
services:
  api:
    ports: !override
      - "127.0.0.1:\${API_PORT:-$API_PORT}:8000"
    environment:
      - STORAGE_ENDPOINT=milvus-minio:9000
      - STORAGE_ACCESS_KEY=\${MINIO_ROOT_USER:-minioadmin}
      - STORAGE_SECRET_KEY=\${MINIO_ROOT_PASSWORD:-minioadmin}
      - HIERARCHY_STORAGE_PATH=/app/storage/hierarchies
    volumes:
      - hierarchy-data:/app/storage/hierarchies

  task-worker:
    environment:
      - STORAGE_ENDPOINT=milvus-minio:9000
      - STORAGE_ACCESS_KEY=\${MINIO_ROOT_USER:-minioadmin}
      - STORAGE_SECRET_KEY=\${MINIO_ROOT_PASSWORD:-minioadmin}
      - HIERARCHY_STORAGE_PATH=/app/storage/hierarchies
    volumes:
      - hierarchy-data:/app/storage/hierarchies

  milvus:
    ports: !override
      - "127.0.0.1:19530:19530"
      - "127.0.0.1:9091:9091"
    environment:
      MINIO_ACCESS_KEY_ID: \${MINIO_ROOT_USER:-minioadmin}
      MINIO_SECRET_ACCESS_KEY: \${MINIO_ROOT_PASSWORD:-minioadmin}

  milvus-minio:
    ports: !override
      - "127.0.0.1:9001:9001"
      - "127.0.0.1:9000:9000"

  postgres:
    ports: !override
      - "127.0.0.1:5432:5432"

  elasticsearch:
    ports: !override
      - "127.0.0.1:\${ELASTICSEARCH_PORT:-9200}:9200"

  trace-dashboard:
    profiles:
      - trace-dashboard
    ports: !override
      - "127.0.0.1:\${TRACE_DASHBOARD_PORT:-8501}:8501"

  web:
    ports: !override
      - "\${WEB_PORT:-$WEB_PORT}:80"
    volumes:
      - $SHARED_DIR/app-config.js:/usr/share/nginx/html/app-config.js:ro

volumes:
  hierarchy-data:
    driver: local
YAML
```

### 9.6 创建 compose 辅助脚本

```bash
cat > "$SHARED_DIR/dc-openrag" <<EOF
#!/usr/bin/env bash
set -euo pipefail
SERVER_HOME="$SERVER_HOME"
SHARED_DIR="\$SERVER_HOME/shared"
CURRENT_LINK="\$SERVER_HOME/current"
exec docker compose -p openrag \\
  --env-file "\$SHARED_DIR/openrag.env" \\
  -f "\$CURRENT_LINK/docker/docker-compose.prod.yml" \\
  -f "\$SHARED_DIR/docker-compose.server.yml" \\
  "\$@"
EOF
chmod +x "$SHARED_DIR/dc-openrag"

cat > "$SHARED_DIR/migrate-openrag" <<EOF
#!/usr/bin/env bash
set -euo pipefail
SERVER_HOME="$SERVER_HOME"
SHARED_DIR="\$SERVER_HOME/shared"
CURRENT_LINK="\$SERVER_HOME/current"
exec docker compose -p openrag \\
  --env-file "\$SHARED_DIR/openrag.env" \\
  -f "\$CURRENT_LINK/docker/docker-compose.prod.yml" \\
  -f "\$SHARED_DIR/docker-compose.server.yml" \\
  run --rm --no-deps api python -m alembic upgrade head
EOF
chmod +x "$SHARED_DIR/migrate-openrag"
```

### 9.7 校验合并后的 Compose

```bash
"$SHARED_DIR/dc-openrag" config --quiet
"$SHARED_DIR/dc-openrag" config --services
"$SHARED_DIR/dc-openrag" config --images
```

如果这里报 `ports: !override` 不支持，先升级 Docker Compose。无法升级时，不要继续启动，改用单独的降级方案处理端口覆盖。

### 9.8 数据库迁移与启动

```bash
"$SHARED_DIR/dc-openrag" up -d --no-build postgres
"$SHARED_DIR/migrate-openrag"
"$SHARED_DIR/dc-openrag" up -d --no-build
```

启动后检查：

```bash
"$SHARED_DIR/dc-openrag" ps
curl -i http://127.0.0.1/health
curl -i http://127.0.0.1/api/health
curl -i "http://127.0.0.1:${API_PORT}/health"
curl -i http://127.0.0.1/app-config.js
```

## 10. 端口建议

建议对外开放：

```text
80: Web 入口，内网用户访问 OpenRag
```

建议仅绑定本机或不对外开放：

```text
18001: API 直连健康检查端口
5432: PostgreSQL
9000/9001: MinIO
9200: Elasticsearch
19530/9091: Milvus
```

Web 前端通过同源路径访问 API：

```text
/api
```

登录请求应发往：

```text
/api/users/login
```

不应发往：

```text
localhost:8001/users/login
```

## 11. 部署成功判定

满足以下条件后，才算部署完成：

- 所有核心容器处于 running 或 healthy。
- `http://127.0.0.1/health` 返回 200。
- `http://127.0.0.1/api/health` 返回 200。
- `http://127.0.0.1:<API_PORT>/health` 返回 200。
- `http://127.0.0.1/app-config.js` 返回 200，内容包含 `apiBaseUrl: "/api"`。
- 浏览器访问 `http://<服务器IP>/` 能打开前端。
- 登录请求发往 `/api/users/login`。
- API / Worker 使用 `STORAGE_ENDPOINT=milvus-minio:9000`。
- Milvus 使用 `MINIO_ADDRESS=milvus-minio:9000`。
- Alembic 迁移能在 API 镜像内执行。
