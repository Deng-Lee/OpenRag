# OpenRag 外网 Docker Compose 部署指南

本文用于把当前本地仓库部署到一台外网 Linux 服务器，部署方式为 Docker Compose。

## 0. 部署口径

### 假设

- 本地构建机是 Windows，仓库路径为 `E:\project\OpenRag`。
- 服务器是 Linux，已经能通过 SSH/SCP 访问。
- 服务器已安装 Docker Engine 和 Docker Compose plugin。
- 不假设服务器可以稳定访问 Docker Hub、Debian 源、npm、pip。
- 镜像在本地构建，用 `docker save` 导出后上传到服务器，服务器只执行 `docker load` 和 `docker compose up --no-build`。
- 默认服务器目录为 `/home/guozhi/Documents/OpenRag`，可按实际用户修改。
- 默认 Web 对外端口为 `80`。
- 默认 API 仅绑定服务器本机 `127.0.0.1:18001`，外部浏览器通过 Web Nginx 的 `/api` 访问 API。

### 不使用的现有入口

不要直接执行：

```bash
cd docker
./deploy.sh
```

原因：

- 当前 `docker/deploy.sh` 语法校验失败，`bash -n docker/deploy.sh` 会报 `unexpected end of file`。
- 该脚本会在服务器上执行 `docker-compose build`，与本次“本地构建、服务器离线加载”的部署口径不一致。

### 部署完成判定

只有同时满足以下条件，才算部署完成：

- Compose 配置能通过 `docker compose config`。
- 所需镜像已全部 `docker load` 到服务器。
- `web`、`api`、`postgres`、`milvus`、`milvus-etcd`、`milvus-minio`、`elasticsearch`、`task-worker` 正常运行。
- `http://127.0.0.1/health` 返回 `200`。
- `http://127.0.0.1/api/health` 返回 `200`。
- `http://127.0.0.1:<API_PORT>/health` 返回 `200`。
- `http://127.0.0.1/app-config.js` 返回 `200`，内容包含 `apiBaseUrl: "/api"`。
- 浏览器访问 `http://<server-ip>/` 能打开前端。
- 登录请求发往 `/api/users/login`，不是 `localhost:8001/users/login`。

## 1. 本地准备变量

### 执行位置

在本地 Windows PowerShell 中执行，工作目录是仓库根目录：

```powershell
cd E:\project\OpenRag

$Version = "1.0.1"
$SshTarget = "guozhi@192.168.100.33"
$ServerHome = "/home/guozhi/Documents/OpenRag"
$ApiPort = "18001"
$WebPort = "80"
```

### 预期

- `$Version` 不带 `V` 前缀，例如使用 `1.0.1`，不要使用 `V1.0.1`。
- `$SshTarget` 可以 SSH 登录服务器。
- `$ServerHome` 是服务器上用于部署 OpenRag 的目录。

### 验证

```powershell
ssh $SshTarget "echo ssh-ok && uname -a"
```

### 验证预期

输出包含：

```text
ssh-ok
```

并显示服务器内核信息。

## 2. 本地检查仓库和环境文件

### 执行指令

```powershell
git status --short

$RequiredFiles = @(
  "docker/docker-compose.prod.yml",
  "docker/Dockerfile.api",
  "docker/Dockerfile.worker",
  "docker/Dockerfile.web",
  "tools/trace_dashboard/Dockerfile",
  "docker/.env",
  "skills/deploy-openrag-server/scripts/package-openrag-release.ps1"
)

foreach ($path in $RequiredFiles) {
  if (!(Test-Path $path)) {
    throw "Missing required file: $path"
  }
}

$RequiredEnvKeys = @(
  "POSTGRES_PASSWORD",
  "SECRET_KEY",
  "MINIO_ROOT_USER",
  "MINIO_ROOT_PASSWORD",
  "OPENAI_API_KEY",
  "OPENAI_BASE_URL",
  "EMBEDDING_MODEL",
  "EMBEDDING_DIMENSION",
  "WEB_PORT"
)

$envMap = @{}
Get-Content -Encoding utf8 docker\.env |
  Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } |
  ForEach-Object {
    $parts = $_ -split '=', 2
    $envMap[$parts[0].Trim()] = if ($parts.Count -gt 1) { $parts[1] } else { "" }
  }

foreach ($key in $RequiredEnvKeys) {
  $status = if (-not $envMap.ContainsKey($key)) {
    "MISSING"
  } elseif ([string]::IsNullOrWhiteSpace($envMap[$key])) {
    "EMPTY"
  } else {
    "SET"
  }
  "{0}={1}" -f $key, $status
  if ($status -ne "SET") {
    throw "docker/.env key is not ready: $key"
  }
}
```

### 预期

- 必需文件都存在。
- 关键 `.env` 项只输出 `SET`，不输出真实密钥。

### 验证

如果 `docker/.env` 不存在，先执行：

```powershell
Copy-Item docker\.env.example docker\.env
notepad docker\.env
```

至少修改这些值：

```dotenv
POSTGRES_PASSWORD=<强密码>
SECRET_KEY=<32位以上随机字符串>
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=<强密码>
OPENAI_API_KEY=<模型网关或 OpenAI 兼容接口 key>
OPENAI_BASE_URL=<模型网关或 OpenAI 兼容接口地址>
EMBEDDING_MODEL=<实际 embedding 模型>
EMBEDDING_DIMENSION=<实际 embedding 维度>
WEB_PORT=80
ELASTICSEARCH__ENABLED=true
ELASTICSEARCH__HOSTS=http://elasticsearch:9200
```

### 验证预期

重新执行本节检查脚本，所有关键项都是：

```text
SET
```

## 3. 本地校验 Compose 配置

### 执行指令

```powershell
docker --version
docker compose version
docker compose -p openrag --env-file docker\.env -f docker\docker-compose.prod.yml config --quiet
docker compose -p openrag --env-file docker\.env -f docker\docker-compose.prod.yml config --services
docker compose -p openrag --env-file docker\.env -f docker\docker-compose.prod.yml config --images
```

### 预期

- `config --quiet` 无输出且退出码为 `0`。
- 服务列表包含：

```text
api
web
task-worker
trace-dashboard
postgres
milvus
milvus-etcd
milvus-minio
elasticsearch
```

- 镜像列表包含：

```text
openrag-api
openrag-web
openrag-task-worker
openrag-trace-dashboard
postgres:16-alpine
quay.io/coreos/etcd:v3.5.5
minio/minio:RELEASE.2023-03-20T20-16-18Z
milvusdb/milvus:v2.4.17
docker.elastic.co/elasticsearch/elasticsearch:8.12.2
```

### 验证预期

如果 `config --quiet` 报错，先修复 Compose 或 `.env`，不要继续打包。

## 4. 创建本地打包脚本

现有 `skills/deploy-openrag-server/scripts/package-openrag-release.ps1` 可以打源码包并构建 `api/web/task-worker`，但当前生产 Compose 还包含 `trace-dashboard`。因此本指南使用一个包装脚本，调用现有脚本并补齐 `trace-dashboard` 镜像和完整镜像包。

### 执行指令

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null

@'
param(
  [Parameter(Mandatory = $true)]
  [string]$Version
)

$ErrorActionPreference = "Stop"

if ($Version -match '^[Vv]') {
  throw "Do not prefix the version with V. Use 1.0.1, not V1.0.1."
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ArtifactsDir = Join-Path $RepoRoot "artifacts"
$ImageTarName = "openrag-images-$Version.tar"
$ImageTarPath = Join-Path $ArtifactsDir $ImageTarName

Push-Location $RepoRoot
try {
  if (!(Test-Path "docker\.env")) {
    throw "docker/.env not found. Copy docker/.env.example to docker/.env and configure it first."
  }

  Write-Host "[1/6] Validate compose config"
  docker compose -p openrag --env-file docker/.env -f docker/docker-compose.prod.yml config --quiet
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose config failed"
  }

  Write-Host "[2/6] Package source and build api/web/task-worker"
  .\skills\deploy-openrag-server\scripts\package-openrag-release.ps1 -Version $Version -SkipImageSave

  Write-Host "[3/6] Build trace-dashboard image"
  docker compose -p openrag --env-file docker/.env -f docker/docker-compose.prod.yml build trace-dashboard
  if ($LASTEXITCODE -ne 0) {
    throw "trace-dashboard build failed"
  }

  Write-Host "[4/6] Pull third-party runtime images"
  $ThirdPartyImages = @(
    "postgres:16-alpine",
    "quay.io/coreos/etcd:v3.5.5",
    "minio/minio:RELEASE.2023-03-20T20-16-18Z",
    "milvusdb/milvus:v2.4.17",
    "docker.elastic.co/elasticsearch/elasticsearch:8.12.2"
  )

  foreach ($image in $ThirdPartyImages) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
      docker pull $image
      if ($LASTEXITCODE -ne 0) {
        throw "Failed to pull image: $image"
      }
    }
  }

  Write-Host "[5/6] Verify all images exist"
  $RequiredImages = @(
    "openrag-api",
    "openrag-web",
    "openrag-task-worker",
    "openrag-trace-dashboard",
    "postgres:16-alpine",
    "quay.io/coreos/etcd:v3.5.5",
    "minio/minio:RELEASE.2023-03-20T20-16-18Z",
    "milvusdb/milvus:v2.4.17",
    "docker.elastic.co/elasticsearch/elasticsearch:8.12.2"
  )

  foreach ($image in $RequiredImages) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
      throw "Missing image before docker save: $image"
    }
  }

  Write-Host "[6/6] Save complete image tar"
  if (Test-Path $ImageTarPath) {
    Remove-Item -LiteralPath $ImageTarPath -Force
  }

  docker save -o $ImageTarPath $RequiredImages
  if ($LASTEXITCODE -ne 0) {
    throw "docker save failed"
  }

  $ImageHash = (Get-FileHash $ImageTarPath -Algorithm SHA256).Hash.ToLower()
  "$ImageHash  $ImageTarName" | Set-Content -Encoding ascii "$ImageTarPath.sha256"

  Write-Host "Release artifacts:"
  Get-ChildItem $ArtifactsDir -Filter "*$Version*" | Select-Object Length, Name
}
finally {
  Pop-Location
}
'@ | Set-Content -Encoding utf8 artifacts\build-openrag-compose-release.ps1
```

### 预期

生成本地脚本：

```text
artifacts/build-openrag-compose-release.ps1
```

### 验证

```powershell
$errors = $null
$tokens = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path artifacts\build-openrag-compose-release.ps1),
  [ref]$tokens,
  [ref]$errors
) | Out-Null

if ($errors) {
  $errors
  throw "build script syntax failed"
}

"build script syntax OK"
```

### 验证预期

输出：

```text
build script syntax OK
```

## 5. 本地构建和打包制品

### 执行脚本

```powershell
.\artifacts\build-openrag-compose-release.ps1 -Version $Version
```

### 脚本执行内容

该脚本会执行：

- `docker compose config --quiet`
- `skills/deploy-openrag-server/scripts/package-openrag-release.ps1 -Version <version> -SkipImageSave`
- `docker compose build trace-dashboard`
- `docker pull` 所需第三方镜像
- `docker image inspect` 校验全部镜像存在
- `docker save` 导出完整镜像包
- `Get-FileHash` 生成镜像包 sha256

### 预期

`artifacts/` 下生成：

```text
openrag-<version>-src.zip
openrag-<version>-src.zip.sha256
openrag-<version>-manifest.json
openrag-images-<version>.tar
openrag-images-<version>.tar.sha256
```

### 验证

```powershell
Get-ChildItem artifacts -Filter "*$Version*" | Select-Object Length, Name

$RequiredArtifacts = @(
  "artifacts/openrag-$Version-src.zip",
  "artifacts/openrag-$Version-src.zip.sha256",
  "artifacts/openrag-$Version-manifest.json",
  "artifacts/openrag-images-$Version.tar",
  "artifacts/openrag-images-$Version.tar.sha256"
)

foreach ($path in $RequiredArtifacts) {
  if (!(Test-Path $path)) {
    throw "Missing artifact: $path"
  }
}

docker image inspect openrag-api | Out-Null
docker image inspect openrag-web | Out-Null
docker image inspect openrag-task-worker | Out-Null
docker image inspect openrag-trace-dashboard | Out-Null
```

### 验证预期

- 5 个制品文件都存在。
- 4 个自构建镜像都能 `inspect`。

## 6. 本地校验 Worker 离线模型

### 执行指令

```powershell
docker run --rm openrag-task-worker sh -c "ls -lh /app/rag/res/deepdoc && for f in det.onnx rec.onnx ocr.res layout.onnx tsr.onnx updown_concat_xgb.model; do test -s /app/rag/res/deepdoc/$f || exit 1; done"
```

### 预期

容器内存在完整 DeepDoc / RAGFlow OCR 模型文件。

### 验证预期

命令退出码为 `0`，输出中能看到：

```text
det.onnx
rec.onnx
ocr.res
layout.onnx
tsr.onnx
updown_concat_xgb.model
```

如果失败，不要上传；先准备模型后重新构建 Worker 镜像。

## 7. 上传制品到服务器

### 执行指令

```powershell
ssh $SshTarget "mkdir -p '$ServerHome/artifacts' '$ServerHome/releases' '$ServerHome/shared' '$ServerHome/backups'"

scp "artifacts\openrag-$Version-src.zip" "$SshTarget`:$ServerHome/artifacts/"
scp "artifacts\openrag-$Version-src.zip.sha256" "$SshTarget`:$ServerHome/artifacts/"
scp "artifacts\openrag-$Version-manifest.json" "$SshTarget`:$ServerHome/artifacts/"
scp "artifacts\openrag-images-$Version.tar" "$SshTarget`:$ServerHome/artifacts/"
scp "artifacts\openrag-images-$Version.tar.sha256" "$SshTarget`:$ServerHome/artifacts/"
scp "docker\.env" "$SshTarget`:$ServerHome/shared/openrag.env"
```

### 预期

服务器上有：

```text
<server_home>/artifacts/openrag-<version>-src.zip
<server_home>/artifacts/openrag-<version>-src.zip.sha256
<server_home>/artifacts/openrag-<version>-manifest.json
<server_home>/artifacts/openrag-images-<version>.tar
<server_home>/artifacts/openrag-images-<version>.tar.sha256
<server_home>/shared/openrag.env
```

### 验证

```powershell
ssh $SshTarget "ls -lh '$ServerHome/artifacts' && test -s '$ServerHome/shared/openrag.env' && echo upload-ok"
```

### 验证预期

输出包含：

```text
upload-ok
```

## 8. 服务器前置检查

### 执行位置

SSH 登录服务器：

```powershell
ssh $SshTarget
```

之后在服务器 shell 中执行：

```bash
export DEPLOY_VERSION="1.0.1"
export SERVER_HOME="/home/guozhi/Documents/OpenRag"
export API_PORT="18001"
export WEB_PORT="80"
export TRACE_DASHBOARD_PORT="8501"
```

如果你的值和示例不同，替换为实际值。

### 执行指令

```bash
docker --version
docker compose version

cat >/tmp/openrag-compose-base-check.yml <<'YAML'
services:
  test:
    image: busybox
    ports:
      - "8001:1"
YAML

cat >/tmp/openrag-compose-override-check.yml <<'YAML'
services:
  test:
    ports: !override
      - "127.0.0.1:18001:1"
YAML

docker compose \
  -f /tmp/openrag-compose-base-check.yml \
  -f /tmp/openrag-compose-override-check.yml \
  config >/dev/null
```

### 预期

- Docker 可用。
- Docker Compose 可用。
- Compose 支持 `ports: !override`。

### 验证预期

最后一条命令退出码为 `0`。

如果服务器 Docker Compose 不支持 `!override`，不要继续执行后续步骤；先升级 Docker Compose，或者按本文第 17 节的降级方案处理端口。

## 9. 服务器创建部署脚本

### 执行脚本创建指令

在服务器 shell 中执行：

```bash
mkdir -p "$SERVER_HOME/shared"

cat > "$SERVER_HOME/shared/deploy-openrag-compose-release.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

: "${DEPLOY_VERSION:?DEPLOY_VERSION is required, for example 1.0.1}"

SERVER_HOME="${SERVER_HOME:-/home/guozhi/Documents/OpenRag}"
API_PORT="${API_PORT:-18001}"
WEB_PORT="${WEB_PORT:-80}"
TRACE_DASHBOARD_PORT="${TRACE_DASHBOARD_PORT:-8501}"

ARTIFACT_DIR="$SERVER_HOME/artifacts"
RELEASES_DIR="$SERVER_HOME/releases"
SHARED_DIR="$SERVER_HOME/shared"
BACKUP_DIR="$SERVER_HOME/backups"
RELEASE_DIR="$RELEASES_DIR/openrag-$DEPLOY_VERSION"
CURRENT_LINK="$SERVER_HOME/current"
ENV_FILE="$SHARED_DIR/openrag.env"
SRC_ZIP="$ARTIFACT_DIR/openrag-$DEPLOY_VERSION-src.zip"
IMG_TAR="$ARTIFACT_DIR/openrag-images-$DEPLOY_VERSION.tar"

require_file() {
  local path="$1"
  if [ ! -s "$path" ]; then
    echo "Missing required file: $path" >&2
    exit 1
  fi
}

set_env_var() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

echo "[1/9] Check artifacts"
mkdir -p "$ARTIFACT_DIR" "$RELEASES_DIR" "$SHARED_DIR" "$BACKUP_DIR"
require_file "$SRC_ZIP"
require_file "$SRC_ZIP.sha256"
require_file "$ARTIFACT_DIR/openrag-$DEPLOY_VERSION-manifest.json"
require_file "$IMG_TAR"
require_file "$IMG_TAR.sha256"
require_file "$ENV_FILE"

echo "[2/9] Verify sha256"
cd "$ARTIFACT_DIR"
sha256sum -c "openrag-$DEPLOY_VERSION-src.zip.sha256"
sha256sum -c "openrag-images-$DEPLOY_VERSION.tar.sha256"

echo "[3/9] Prepare runtime env"
chmod 600 "$ENV_FILE"
sed -i 's/\r$//' "$ENV_FILE"
set_env_var "API_PORT" "$API_PORT"
set_env_var "WEB_PORT" "$WEB_PORT"
set_env_var "TRACE_DASHBOARD_PORT" "$TRACE_DASHBOARD_PORT"

echo "[4/9] Extract source release"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
if command -v unzip >/dev/null 2>&1; then
  unzip -q "$SRC_ZIP" -d "$RELEASE_DIR"
elif command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e "$SRC_ZIP" "$RELEASE_DIR"
else
  echo "Neither unzip nor python3 is available to extract $SRC_ZIP" >&2
  exit 1
fi

if [ -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
  mv "$CURRENT_LINK" "$BACKUP_DIR/current-non-symlink-$(date +%Y%m%d%H%M%S)"
fi
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

echo "[5/9] Load Docker images"
docker load -i "$IMG_TAR"

echo "[6/9] Write app-config.js"
cat > "$SHARED_DIR/app-config.js" <<'EOF'
window.__OPENRAG_CONFIG__ = {
  apiBaseUrl: "/api"
};
EOF

echo "[7/9] Write docker-compose.server.yml"
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
    ports: !override
      - "127.0.0.1:\${TRACE_DASHBOARD_PORT:-$TRACE_DASHBOARD_PORT}:8501"

  web:
    ports: !override
      - "\${WEB_PORT:-$WEB_PORT}:80"
    volumes:
      - $SHARED_DIR/app-config.js:/usr/share/nginx/html/app-config.js:ro

volumes:
  hierarchy-data:
    driver: local
YAML

echo "[8/9] Write dc helper"
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

echo "[9/9] Validate merged compose"
"$SHARED_DIR/dc-openrag" config --quiet
"$SHARED_DIR/dc-openrag" config --services
"$SHARED_DIR/dc-openrag" config --images

echo "prepare-release-ok"
echo "Current release: $RELEASE_DIR"
echo "Compose helper: $SHARED_DIR/dc-openrag"
SH

chmod +x "$SERVER_HOME/shared/deploy-openrag-compose-release.sh"
```

### 脚本内容说明

该脚本会执行：

- 检查制品和 `openrag.env`。
- 校验源码包和镜像包 sha256。
- 修正 Windows 上传的 `.env` 换行。
- 写入 `API_PORT`、`WEB_PORT`、`TRACE_DASHBOARD_PORT`。
- 解压源码包到 `releases/openrag-<version>`。
- 把 `current` 指向当前 release。
- `docker load` 导入镜像包。
- 生成 `shared/app-config.js`。
- 生成服务器专用 `shared/docker-compose.server.yml`。
- 生成 `shared/dc-openrag` Compose 辅助脚本。
- 执行 `docker compose config` 校验合并后的配置。

### 预期

生成：

```text
<server_home>/shared/deploy-openrag-compose-release.sh
```

### 验证

```bash
bash -n "$SERVER_HOME/shared/deploy-openrag-compose-release.sh"
```

### 验证预期

命令无输出，退出码为 `0`。

## 10. 服务器执行制品准备

### 执行脚本

```bash
DEPLOY_VERSION="$DEPLOY_VERSION" \
SERVER_HOME="$SERVER_HOME" \
API_PORT="$API_PORT" \
WEB_PORT="$WEB_PORT" \
TRACE_DASHBOARD_PORT="$TRACE_DASHBOARD_PORT" \
"$SERVER_HOME/shared/deploy-openrag-compose-release.sh"
```

### 预期

输出最后包含：

```text
prepare-release-ok
```

并显示：

```text
Current release: <server_home>/releases/openrag-<version>
Compose helper: <server_home>/shared/dc-openrag
```

### 验证

```bash
test -L "$SERVER_HOME/current"
test -s "$SERVER_HOME/shared/app-config.js"
test -s "$SERVER_HOME/shared/docker-compose.server.yml"
test -x "$SERVER_HOME/shared/dc-openrag"
"$SERVER_HOME/shared/dc-openrag" config --quiet
```

### 验证预期

全部命令退出码为 `0`。

## 11. 服务器确认镜像已导入

### 执行指令

```bash
docker image inspect openrag-api >/dev/null
docker image inspect openrag-web >/dev/null
docker image inspect openrag-task-worker >/dev/null
docker image inspect openrag-trace-dashboard >/dev/null
docker image inspect postgres:16-alpine >/dev/null
docker image inspect quay.io/coreos/etcd:v3.5.5 >/dev/null
docker image inspect minio/minio:RELEASE.2023-03-20T20-16-18Z >/dev/null
docker image inspect milvusdb/milvus:v2.4.17 >/dev/null
docker image inspect docker.elastic.co/elasticsearch/elasticsearch:8.12.2 >/dev/null
```

### 预期

所有镜像都存在。

### 验证预期

全部命令无错误，退出码为 `0`。

## 12. 启动服务

### 执行指令

```bash
"$SERVER_HOME/shared/dc-openrag" up -d --no-build
```

### 预期

Compose 只使用已加载镜像启动服务，不在服务器上构建镜像。

### 验证

```bash
"$SERVER_HOME/shared/dc-openrag" ps
```

### 验证预期

至少看到以下容器处于 `running` 或 `healthy`：

```text
openrag-web-prod
openrag-api-prod
openrag-task-worker
openrag-postgres-prod
openrag-milvus-prod
openrag-milvus-etcd-prod
openrag-milvus-minio-prod
openrag-elasticsearch-prod
openrag-trace-dashboard-prod
```

`task-worker` 没有独立 healthcheck 时，只要状态是 `running` 即可。

## 13. 健康检查

### 执行指令

```bash
curl -i http://127.0.0.1/health
curl -i http://127.0.0.1/api/health
curl -i "http://127.0.0.1:${API_PORT}/health"
curl -i http://127.0.0.1/app-config.js
```

### 预期

- Web 自身健康检查通过。
- Web Nginx 能通过 `/api/health` 代理到 API。
- API 直连端口健康检查通过。
- 前端运行时配置文件存在。

### 验证预期

前三条返回 HTTP `200`。

`app-config.js` 返回 HTTP `200`，正文包含：

```javascript
window.__OPENRAG_CONFIG__ = {
  apiBaseUrl: "/api"
};
```

## 14. 检查关键运行时配置

### 执行指令

```bash
docker exec openrag-api-prod env | grep -E 'POSTGRES_HOST|MILVUS_HOST|ELASTICSEARCH__HOSTS|STORAGE_ENDPOINT|STORAGE_ACCESS_KEY|STORAGE_SECRET_KEY|OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|HIERARCHY_STORAGE_PATH'

docker exec openrag-task-worker env | grep -E 'WORKER_API_URL|POSTGRES_HOST|MILVUS_HOST|ELASTICSEARCH__HOSTS|STORAGE_ENDPOINT|STORAGE_ACCESS_KEY|STORAGE_SECRET_KEY|OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|HIERARCHY_STORAGE_PATH|OPENRAG_PDF_BACKEND'

docker exec openrag-milvus-prod env | grep -E 'MINIO_ADDRESS|MINIO_ACCESS_KEY_ID|MINIO_SECRET_ACCESS_KEY'
```

### 预期

API 和 Worker 使用 Compose 内部服务名访问依赖。

### 验证预期

应能看到：

```text
POSTGRES_HOST=postgres
MILVUS_HOST=milvus
ELASTICSEARCH__HOSTS=http://elasticsearch:9200
STORAGE_ENDPOINT=milvus-minio:9000
HIERARCHY_STORAGE_PATH=/app/storage/hierarchies
WORKER_API_URL=http://api:8000
MINIO_ADDRESS=milvus-minio:9000
```

`STORAGE_ACCESS_KEY`、`STORAGE_SECRET_KEY`、`MINIO_ACCESS_KEY_ID`、`MINIO_SECRET_ACCESS_KEY` 应存在；不要把真实值贴到公开渠道。

## 15. 检查日志

### 执行指令

```bash
"$SERVER_HOME/shared/dc-openrag" logs --tail=120 api
"$SERVER_HOME/shared/dc-openrag" logs --tail=120 web
"$SERVER_HOME/shared/dc-openrag" logs --tail=120 task-worker
"$SERVER_HOME/shared/dc-openrag" logs --tail=120 milvus
"$SERVER_HOME/shared/dc-openrag" logs --tail=120 elasticsearch
```

### 预期

没有持续重启、连接失败、MinIO AccessDenied、数据库认证失败、模型文件缺失等错误。

### 验证预期

API 日志中应出现类似：

```text
Database connection verified
OpenRag API started
```

Worker 日志不应出现：

```text
not find model file
AccessDenied
Connection refused
```

## 16. 浏览器验收

### 执行指令

在浏览器访问：

```text
http://<server-ip>/
```

打开浏览器开发者工具，尝试登录或注册。

### 预期

- 前端页面能加载。
- Network 中能看到 `/app-config.js` 请求成功。
- 登录请求路径是 `/api/users/login`。

### 验证预期

如果登录返回 `401`，说明前端到后端链路已经通了，但账号或密码不正确。

如果没有账号，可以访问：

```text
http://<server-ip>/register
```

然后注册首个账号。

## 17. Docker Compose 不支持 `!override` 时的降级方案

优先建议升级 Docker Compose。只有无法升级时，才使用本节。

### 操作

把 `shared/docker-compose.server.yml` 中所有 `ports: !override` 改成普通 `ports:`，然后手动 patch 当前 release 的基础 Compose 文件，避免重复暴露端口。

执行：

```bash
CURRENT_COMPOSE="$SERVER_HOME/current/docker/docker-compose.prod.yml"

sed -i 's|"8001:8000"|"127.0.0.1:'"$API_PORT"':8000"|' "$CURRENT_COMPOSE"
sed -i 's|- "5432:5432"|- "127.0.0.1:5432:5432"|' "$CURRENT_COMPOSE"
sed -i 's|- "9001:9001"|- "127.0.0.1:9001:9001"|' "$CURRENT_COMPOSE"
sed -i 's|- "9000:9000"|- "127.0.0.1:9000:9000"|' "$CURRENT_COMPOSE"
sed -i 's|- "19530:19530"|- "127.0.0.1:19530:19530"|' "$CURRENT_COMPOSE"
sed -i 's|- "9091:9091"|- "127.0.0.1:9091:9091"|' "$CURRENT_COMPOSE"
sed -i 's|- "${ELASTICSEARCH_PORT:-9200}:9200"|- "127.0.0.1:${ELASTICSEARCH_PORT:-9200}:9200"|' "$CURRENT_COMPOSE"
sed -i 's|- "${TRACE_DASHBOARD_PORT:-8501}:8501"|- "127.0.0.1:${TRACE_DASHBOARD_PORT:-8501}:8501"|' "$CURRENT_COMPOSE"

sed -i 's|ports: !override|ports:|g' "$SERVER_HOME/shared/docker-compose.server.yml"

"$SERVER_HOME/shared/dc-openrag" config --quiet
```

### 预期

`docker compose config --quiet` 通过。

### 验证预期

```bash
"$SERVER_HOME/shared/dc-openrag" config | grep -E '127.0.0.1.*5432|127.0.0.1.*9000|127.0.0.1.*19530|127.0.0.1.*9200|127.0.0.1.*8501'
```

能看到中间件端口都绑定在 `127.0.0.1`。

## 18. 常见问题定位

### `docker load` 后启动仍然 build

检查启动命令是否包含：

```bash
--no-build
```

正确命令是：

```bash
"$SERVER_HOME/shared/dc-openrag" up -d --no-build
```

### `/api/health` 不通

执行：

```bash
"$SERVER_HOME/shared/dc-openrag" ps api web
"$SERVER_HOME/shared/dc-openrag" logs --tail=120 api
"$SERVER_HOME/shared/dc-openrag" logs --tail=120 web
curl -i "http://127.0.0.1:${API_PORT}/health"
```

预期：

- API 是 running 或 healthy。
- Web 是 running 或 healthy。
- 直连 API 健康检查返回 `200`。

### `/app-config.js` 404

检查挂载：

```bash
test -s "$SERVER_HOME/shared/app-config.js"
"$SERVER_HOME/shared/dc-openrag" config | grep app-config.js
"$SERVER_HOME/shared/dc-openrag" up -d --no-build web
curl -i http://127.0.0.1/app-config.js
```

预期返回 `200`。

### MinIO 或 Milvus AccessDenied

检查：

```bash
docker exec openrag-api-prod env | grep STORAGE_
docker exec openrag-task-worker env | grep STORAGE_
docker exec openrag-milvus-prod env | grep MINIO
```

预期：

```text
STORAGE_ENDPOINT=milvus-minio:9000
MINIO_ADDRESS=milvus-minio:9000
```

并且 API、Worker、Milvus 使用同一组 MinIO 用户和密码。

### 登录返回 401

含义：网络链路通了，但认证失败。

检查用户表：

```bash
docker exec openrag-postgres-prod psql -U openrag -d openrag -c "select id, username, email, is_active, is_admin from users;"
```

如果没有用户，先访问：

```text
http://<server-ip>/register
```

### Worker 提示模型文件缺失

说明本地构建的 Worker 镜像没有包含完整离线模型。

回到本地构建机执行：

```powershell
docker run --rm openrag-task-worker sh -c "ls -lh /app/rag/res/deepdoc"
```

如果缺文件，先补齐 `openrag/rag/res/deepdoc/` 后重新执行第 5 步。

## 19. 当前仓库的迁移说明

当前 Docker 文档里提到：

```bash
docker-compose -f docker-compose.prod.yml exec api alembic upgrade head
```

但当前 `docker/Dockerfile.api` 没有把 `openrag/alembic.ini` 和 `openrag/alembic/` 复制进 API 镜像。因此本文不把这个命令作为必需步骤。

当前 API 启动时会执行 `Base.metadata.create_all()`，全新空库通常能创建表。但生产升级已有数据库时，仍应补齐正式迁移方案，例如：

- 修改 `docker/Dockerfile.api`，把 `openrag/alembic.ini` 和 `openrag/alembic/` 复制进镜像。
- 或者使用单独的迁移镜像/运维环境执行 `alembic upgrade head`。

完成迁移能力补齐前，不要把 `create_all()` 当成长期生产迁移方案。

## 20. 最终验收清单

执行：

```bash
"$SERVER_HOME/shared/dc-openrag" ps
curl -fsS http://127.0.0.1/health
curl -fsS http://127.0.0.1/api/health
curl -fsS "http://127.0.0.1:${API_PORT}/health"
curl -fsS http://127.0.0.1/app-config.js
docker exec openrag-api-prod env | grep STORAGE_ENDPOINT
docker exec openrag-task-worker env | grep STORAGE_ENDPOINT
docker exec openrag-milvus-prod env | grep MINIO_ADDRESS
```

验收预期：

- `ps` 中核心容器是 running 或 healthy。
- 三个健康检查都成功。
- `app-config.js` 存在。
- API / Worker 的 `STORAGE_ENDPOINT` 是 `milvus-minio:9000`。
- Milvus 的 `MINIO_ADDRESS` 是 `milvus-minio:9000`。
- 浏览器访问 `http://<server-ip>/` 成功。
- 登录请求发往 `/api/users/login`。
