param(
  [Parameter(Mandatory = $true)]
  [string]$Version,

  [string]$SshTarget = "guozhi@192.168.100.33",
  [string]$ServerHome = "/home/guozhi/Documents/OpenRag",
  [string]$ApiPort = "18001",
  [string]$WebPort = "80",
  [string]$EnvFile = "docker\.env",

  [switch]$SkipImageBuild,
  [switch]$ForceThirdPartyImages,
  [switch]$NoUploadThirdPartyImages,
  [switch]$AllowInteractiveSsh,
  [switch]$SkipEnvUpload
)

$ErrorActionPreference = "Stop"

function Invoke-Native {
  param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
  )

  Write-Host ">> $FilePath $($Arguments -join ' ')"
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "命令执行失败，退出码 ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
  }
}

function Require-File {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (!(Test-Path $Path)) {
    throw "缺少必需文件: $Path"
  }
}

if ($Version -match '^[Vv]') {
  throw "版本号不要带 V 前缀。请使用 1.0.1，不要使用 V1.0.1。"
}

$RepoRoot = (Get-Location).Path
$GuidePath = Join-Path $RepoRoot "docs\外网DockerCompose部署指南.md"
if (!(Test-Path $GuidePath)) {
  throw "请从 OpenRag 仓库根目录运行本脚本。未找到部署指南: $GuidePath"
}

Write-Host "OpenRag 发布准备"
Write-Host "Version=$Version"
Write-Host "SshTarget=$SshTarget"
Write-Host "ServerHome=$ServerHome"
Write-Host "ApiPort=$ApiPort"
Write-Host "WebPort=$WebPort"
Write-Host "EnvFile=$EnvFile"

Write-Host "[1/8] 检查本地仓库文件和环境变量"
git status --short

$RequiredFiles = @(
  "docker/docker-compose.prod.yml",
  "docker/Dockerfile.api",
  "docker/Dockerfile.worker",
  "docker/Dockerfile.web",
  "scripts/build-openrag-compose-release.ps1",
  "skills/deploy-openrag-server/scripts/package-openrag-release.ps1"
)

foreach ($path in $RequiredFiles) {
  Require-File $path
}
Require-File $EnvFile

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
Get-Content -Encoding utf8 $EnvFile |
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
    throw "$EnvFile 配置未就绪: $key"
  }
}

Write-Host "[2/8] 检查 SSH 连接"
$SshOptions = @()
if (!$AllowInteractiveSsh) {
  $SshOptions = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
}
Invoke-Native ssh @SshOptions $SshTarget "echo ssh-ok && uname -a"

Write-Host "[3/8] 校验 Docker Compose 配置"
Invoke-Native docker "--version"
Invoke-Native docker "compose" "version"
Invoke-Native docker "compose" "-p" "openrag" "--env-file" $EnvFile "-f" "docker\docker-compose.prod.yml" "config" "--quiet"

$services = & docker compose -p openrag --env-file $EnvFile -f docker\docker-compose.prod.yml config --services
if ($LASTEXITCODE -ne 0) {
  throw "docker compose config --services 执行失败"
}
$requiredServices = @("api", "web", "task-worker", "postgres", "milvus", "milvus-etcd", "milvus-minio", "elasticsearch")
foreach ($service in $requiredServices) {
  if ($services -notcontains $service) {
    throw "Compose 缺少服务: $service"
  }
}
$services

$images = & docker compose -p openrag --env-file $EnvFile -f docker\docker-compose.prod.yml config --images
if ($LASTEXITCODE -ne 0) {
  throw "docker compose config --images 执行失败"
}
$requiredImages = @(
  "openrag-api",
  "openrag-web",
  "openrag-task-worker",
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "minio/minio:RELEASE.2023-03-20T20-16-18Z",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2"
)
foreach ($image in $requiredImages) {
  if ($images -notcontains $image) {
    throw "Compose 缺少镜像: $image"
  }
}
$images

Write-Host "[4/8] 校验本地打包脚本语法"
$errors = $null
$tokens = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path scripts\build-openrag-compose-release.ps1),
  [ref]$tokens,
  [ref]$errors
) | Out-Null
if ($errors) {
  $errors
  throw "本地打包脚本语法校验失败"
}
"本地打包脚本语法 OK"

Write-Host "[5/8] 构建并打包发布制品"
$thirdPartyTar = "artifacts\openrag-third-party-images.tar"
$thirdPartySha = "artifacts\openrag-third-party-images.tar.sha256"
$includeThirdParty = [bool]$ForceThirdPartyImages

$buildArgs = @("-Version", $Version)
if ($SkipImageBuild) {
  $buildArgs += "-SkipImageBuild"
}
if ($includeThirdParty) {
  $buildArgs += "-IncludeThirdPartyImages"
} else {
  Write-Host "默认复用已有第三方镜像包，本次不重新构建第三方镜像。"
}
Invoke-Native ".\scripts\build-openrag-compose-release.ps1" @buildArgs

$RequiredArtifacts = @(
  "artifacts/openrag-$Version-src.zip",
  "artifacts/openrag-$Version-src.zip.sha256",
  "artifacts/openrag-$Version-manifest.json",
  "artifacts/openrag-app-images-$Version.tar",
  "artifacts/openrag-app-images-$Version.tar.sha256"
)
foreach ($path in $RequiredArtifacts) {
  Require-File $path
}

Invoke-Native docker "image" "inspect" "openrag-api"
Invoke-Native docker "image" "inspect" "openrag-web"
Invoke-Native docker "image" "inspect" "openrag-task-worker"
Invoke-Native docker "run" "--rm" "openrag-api" "sh" "-c" "test -f /app/alembic.ini && test -d /app/alembic && python -m alembic --help >/dev/null && echo alembic-ok"

Write-Host "[6/8] 校验 worker 离线模型"
Invoke-Native docker "run" "--rm" "openrag-task-worker" "sh" "-c" 'ls -lh /app/rag/res/deepdoc && for f in det.onnx rec.onnx ocr.res layout.onnx tsr.onnx updown_concat_xgb.model; do test -s /app/rag/res/deepdoc/$f || exit 1; done'

Write-Host "[7/8] 上传制品到服务器"
$ScpOptions = $SshOptions
Invoke-Native ssh @SshOptions $SshTarget "mkdir -p '$ServerHome/artifacts' '$ServerHome/releases' '$ServerHome/shared' '$ServerHome/backups'"

$uploadFiles = @(
  "artifacts\openrag-$Version-src.zip",
  "artifacts\openrag-$Version-src.zip.sha256",
  "artifacts\openrag-$Version-manifest.json",
  "artifacts\openrag-app-images-$Version.tar",
  "artifacts\openrag-app-images-$Version.tar.sha256"
)

foreach ($file in $uploadFiles) {
  Invoke-Native scp @ScpOptions $file "$SshTarget`:$ServerHome/artifacts/"
}

if (!$NoUploadThirdPartyImages -and (Test-Path $thirdPartyTar) -and (Test-Path $thirdPartySha)) {
  Invoke-Native scp @ScpOptions $thirdPartyTar "$SshTarget`:$ServerHome/artifacts/"
  Invoke-Native scp @ScpOptions $thirdPartySha "$SshTarget`:$ServerHome/artifacts/"
} elseif (!$NoUploadThirdPartyImages) {
  Write-Host "未找到本地第三方镜像包，跳过第三方镜像上传；服务器将复用已有第三方镜像。"
}

if (!$SkipEnvUpload) {
  Invoke-Native scp @ScpOptions $EnvFile "$SshTarget`:$ServerHome/shared/openrag.env"
} else {
  Write-Host "跳过 $EnvFile 上传：保留服务器现有 shared/openrag.env（适用于仅代码变更、env 未改的更新）。"
}

Write-Host "[8/8] 校验远端上传结果"
Invoke-Native ssh @SshOptions $SshTarget "ls -lh '$ServerHome/artifacts' && test -s '$ServerHome/shared/openrag.env' && echo upload-ok"

Write-Host "release-prep-upload-ok"
Write-Host "制品目录: $ServerHome/artifacts"
Write-Host "环境文件: $ServerHome/shared/openrag.env"


