param(
  [Parameter(Mandatory = $true)]
  [string]$Version,

  [string]$RepoRoot,

  [switch]$SkipImageBuild,
  [switch]$SkipImageSave,
  [switch]$AppImagesOnly
)

$ErrorActionPreference = "Stop"

if ($Version -match '^[Vv]') {
  throw "Do not prefix the version with V. Use 1.0.1, not V1.0.1."
}

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
  $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$ArtifactsDir = Join-Path $RepoRoot "artifacts"
$PackageName = "openrag-$Version-src.zip"
$PackagePath = Join-Path $ArtifactsDir $PackageName
$ManifestPath = Join-Path $ArtifactsDir "openrag-$Version-manifest.json"

New-Item -ItemType Directory -Force $ArtifactsDir | Out-Null

Push-Location $RepoRoot
try {
  if (!(Test-Path "docker\docker-compose.prod.yml")) {
    throw "docker/docker-compose.prod.yml not found. Run this from the OpenRag repository."
  }
  if (!(Test-Path "docker\.env")) {
    throw "docker/.env not found. Runtime config cannot be prepared."
  }

  Write-Host "[1/5] Packaging source: $PackageName"
  if (Test-Path $PackagePath) {
    Remove-Item -LiteralPath $PackagePath -Force
  }

  & tar.exe -a -cf $PackagePath `
    --exclude=".git" `
    --exclude=".worktrees" `
    --exclude=".claude" `
    --exclude=".pytest_cache" `
    --exclude=".uv-cache*" `
    --exclude="venv" `
    --exclude=".venv" `
    --exclude="openrag/venv" `
    --exclude="web/node_modules" `
    --exclude="web/dist" `
    --exclude="docker/.env" `
    --exclude="openrag/.env" `
    --exclude="web/.env" `
    --exclude="artifacts" `
    --exclude="backups" `
    "."

  if ($LASTEXITCODE -ne 0) {
    throw "tar failed with exit code $LASTEXITCODE"
  }

  $EnvMatches = & tar.exe -tf $PackagePath | Select-String -Pattern '(^|/)(docker|openrag|web)/\.env$'
  if ($EnvMatches) {
    throw "Source package contains .env files. Matches: $EnvMatches"
  }

  Write-Host "[2/5] Writing source sha256 and manifest"
  $SourceHash = (Get-FileHash $PackagePath -Algorithm SHA256).Hash.ToLower()
  "$SourceHash  $PackageName" | Set-Content -Encoding ascii "$PackagePath.sha256"

  $GitCommit = "unknown"
  try {
    $GitCommit = (& git rev-parse HEAD).Trim()
  } catch {
    $GitCommit = "unknown"
  }

  $Manifest = [ordered]@{
    version = $Version
    package = $PackageName
    sha256 = $SourceHash
    built_at = (Get-Date).ToString("o")
    git_commit = $GitCommit
    docker_compose = "docker/docker-compose.prod.yml"
    runtime_env = "/home/guozhi/Documents/OpenRag/shared/openrag.env"
    server_path = "/home/guozhi/Documents/OpenRag"
    note = "Source package excludes docker/.env, openrag/.env and web/.env. Runtime config is shared/openrag.env on server."
  }
  $Manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $ManifestPath

  if (-not $SkipImageBuild) {
    Write-Host "[3/5] Building application images locally"
    & docker compose -p openrag --env-file docker/.env -f docker/docker-compose.prod.yml build api web task-worker
    if ($LASTEXITCODE -ne 0) {
      throw "docker compose build failed with exit code $LASTEXITCODE"
    }
  } else {
    Write-Host "[3/5] Skipping image build"
  }

  if (-not $SkipImageSave) {
    Write-Host "[4/5] Saving Docker images"
    if ($AppImagesOnly) {
      $Images = @("openrag-api", "openrag-web", "openrag-task-worker")
      $ImageTarName = "openrag-app-images-$Version.tar"
    } else {
      $Images = @(
        "openrag-api",
        "openrag-web",
        "openrag-task-worker",
        "postgres:16-alpine",
        "quay.io/coreos/etcd:v3.5.5",
        "minio/minio:RELEASE.2023-03-20T20-16-18Z",
        "milvusdb/milvus:v2.4.17",
        "docker.elastic.co/elasticsearch/elasticsearch:8.12.2"
      )
      $ImageTarName = "openrag-images-$Version.tar"
    }

    $ImageTarPath = Join-Path $ArtifactsDir $ImageTarName
    if (Test-Path $ImageTarPath) {
      Remove-Item -LiteralPath $ImageTarPath -Force
    }

    & docker save -o $ImageTarPath $Images
    if ($LASTEXITCODE -ne 0) {
      throw "docker save failed with exit code $LASTEXITCODE"
    }

    $ImageHash = (Get-FileHash $ImageTarPath -Algorithm SHA256).Hash.ToLower()
    "$ImageHash  $ImageTarName" | Set-Content -Encoding ascii "$ImageTarPath.sha256"
  } else {
    Write-Host "[4/5] Skipping image save"
  }

  Write-Host "[5/5] Artifact list"
  Get-ChildItem $ArtifactsDir -Filter "*$Version*" | Select-Object Length, Name
}
finally {
  Pop-Location
}
