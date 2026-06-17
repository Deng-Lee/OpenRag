param(
  [Parameter(Mandatory = $true)]
  [string]$Version,

  [switch]$SkipImageBuild,
  [switch]$IncludeThirdPartyImages
)

$ErrorActionPreference = "Stop"

if ($Version -match '^[Vv]') {
  throw "Do not prefix the version with V. Use 1.0.1, not V1.0.1."
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ArtifactsDir = Join-Path $RepoRoot "artifacts"
$AppTarName = "openrag-app-images-$Version.tar"
$AppTarPath = Join-Path $ArtifactsDir $AppTarName
$ThirdPartyTarName = "openrag-third-party-images.tar"
$ThirdPartyTarPath = Join-Path $ArtifactsDir $ThirdPartyTarName

$ThirdPartyImages = @(
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "minio/minio:RELEASE.2023-03-20T20-16-18Z",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2"
)

Push-Location $RepoRoot
try {
  New-Item -ItemType Directory -Force $ArtifactsDir | Out-Null

  if (!(Test-Path "docker\.env")) {
    throw "docker/.env not found. Copy docker/.env.example to docker/.env and configure it first."
  }

  Write-Host "[1/6] Validate compose config"
  docker compose -p openrag --env-file docker/.env -f docker/docker-compose.prod.yml config --quiet
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose config failed"
  }

  Write-Host "[2/6] Package source and build application images"
  if ($SkipImageBuild) {
    & ".\skills\deploy-openrag-server\scripts\package-openrag-release.ps1" -Version $Version -SkipImageSave -SkipImageBuild
  } else {
    & ".\skills\deploy-openrag-server\scripts\package-openrag-release.ps1" -Version $Version -SkipImageSave
  }
  if ($LASTEXITCODE -ne 0) {
    throw "application package failed"
  }

  Write-Host "[3/6] Verify application images"
  $AppImages = @("openrag-api", "openrag-web", "openrag-task-worker")
  foreach ($image in $AppImages) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
      throw "Missing application image: $image"
    }
  }

  Write-Host "[4/6] Save application image package"
  if (Test-Path $AppTarPath) {
    Remove-Item -LiteralPath $AppTarPath -Force
  }

  docker save -o $AppTarPath $AppImages
  if ($LASTEXITCODE -ne 0) {
    throw "docker save application images failed"
  }

  $AppHash = (Get-FileHash $AppTarPath -Algorithm SHA256).Hash.ToLower()
  "$AppHash  $AppTarName" | Set-Content -Encoding ascii "$AppTarPath.sha256"

  if ($IncludeThirdPartyImages) {
    Write-Host "[5/6] Pull and save reusable third-party images"
    foreach ($image in $ThirdPartyImages) {
      docker image inspect $image *> $null
      if ($LASTEXITCODE -ne 0) {
        docker pull $image
        if ($LASTEXITCODE -ne 0) {
          throw "Failed to pull image: $image"
        }
      }
    }

    if (Test-Path $ThirdPartyTarPath) {
      Remove-Item -LiteralPath $ThirdPartyTarPath -Force
    }

    docker save -o $ThirdPartyTarPath $ThirdPartyImages
    if ($LASTEXITCODE -ne 0) {
      throw "docker save third-party images failed"
    }

    $ThirdPartyHash = (Get-FileHash $ThirdPartyTarPath -Algorithm SHA256).Hash.ToLower()
    "$ThirdPartyHash  $ThirdPartyTarName" | Set-Content -Encoding ascii "$ThirdPartyTarPath.sha256"
  } else {
    Write-Host "[5/6] Skipping third-party image package"
  }

  Write-Host "[6/6] Artifact list"
  Get-ChildItem $ArtifactsDir -Filter "*$Version*" | Select-Object Length, Name
  Get-ChildItem $ArtifactsDir -Filter "openrag-third-party-images*" | Select-Object Length, Name
}
finally {
  Pop-Location
}
