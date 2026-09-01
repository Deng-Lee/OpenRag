param(
  [Parameter(Mandatory = $true)]
  [string]$Version,

  [string]$RepoRoot = (Get-Location).Path,
  [string]$BaseRef = "HEAD^",
  [ValidateSet("api", "web", "task-worker")]
  [string[]]$Services = @("api", "web", "task-worker"),
  [switch]$RequiresMigration,
  [switch]$IncludeThirdPartyImages
)

$ErrorActionPreference = "Stop"

function Invoke-Native {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
  )
  Write-Host ">> $FilePath $($Arguments -join ' ')"
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "命令执行失败，退出码 ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
  }
}

function Require-File {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (!(Test-Path -LiteralPath $Path)) {
    throw "缺少必需文件: $Path"
  }
}

if ($Version -match '^[Vv]') {
  throw "版本号不要带 V/v 前缀。"
}
if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]*$') {
  throw "版本号只能包含字母、数字、点、下划线和连字符: $Version"
}
if ($Services.Count -eq 0) {
  throw "至少选择一个应用服务镜像。"
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$ArtifactsDir = Join-Path $RepoRoot "artifacts"
$BundleName = "openrag-offline-$Version"
$BundleDir = Join-Path $ArtifactsDir $BundleName
$OuterTar = Join-Path $ArtifactsDir "$BundleName.tar"
$PackageScript = Join-Path $RepoRoot "skills\deploy-openrag-server\scripts\package-openrag-release.ps1"
$ServerScriptsDir = Join-Path $RepoRoot "scripts\intranet-compose"
$GuidePath = Join-Path $RepoRoot "docs\内网DockerCompose离线部署指南.md"
$EnvFile = Join-Path $RepoRoot "docker\.env"

Push-Location $RepoRoot
try {
  Write-Host "[1/9] 校验分支、工作区和输入"
  $branch = (& git branch --show-current).Trim()
  if ($branch -ne "deploy-main") {
    throw "当前分支是 $branch；必须在 deploy-main 的干净 worktree 中构建。"
  }
  $dirty = @(& git status --short)
  if ($dirty.Count -gt 0) {
    throw "工作区不干净，拒绝把未提交内容放入发布包:`n$($dirty -join "`n")"
  }
  Invoke-Native git "rev-parse" "--verify" $BaseRef
  Require-File $PackageScript
  Require-File $GuidePath
  Require-File $EnvFile
  Require-File (Join-Path $ServerScriptsDir "deploy-openrag-offline.sh")
  Require-File (Join-Path $RepoRoot "docker\docker-compose.prod.yml")
  if (Test-Path -LiteralPath $BundleDir) {
    throw "离线包目录已经存在，拒绝覆盖不可变制品: $BundleDir"
  }
  if (Test-Path -LiteralPath $OuterTar) {
    throw "离线包文件已经存在，拒绝覆盖不可变制品: $OuterTar"
  }

  Write-Host "[2/9] 校验运行时配置（只显示状态，不显示密钥）"
  $envMap = @{}
  Get-Content -LiteralPath $EnvFile -Encoding utf8 |
    Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } |
    ForEach-Object {
      $parts = $_ -split '=', 2
      $envMap[$parts[0].Trim()] = if ($parts.Count -gt 1) { $parts[1] } else { "" }
    }
  $requiredEnvKeys = @(
    "POSTGRES_PASSWORD", "SECRET_KEY", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSION"
  )
  foreach ($key in $requiredEnvKeys) {
    $status = if (!$envMap.ContainsKey($key)) {
      "MISSING"
    } elseif ([string]::IsNullOrWhiteSpace($envMap[$key])) {
      "EMPTY"
    } else {
      "SET"
    }
    "$key=$status"
    if ($status -ne "SET") {
      throw "docker/.env 未就绪: $key=$status"
    }
  }

  Write-Host "[3/9] 校验 Docker Compose"
  Invoke-Native docker "--version"
  Invoke-Native docker "compose" "version"
  Invoke-Native docker "compose" "-p" "openrag" "--env-file" "docker/.env" "-f" "docker/docker-compose.prod.yml" "config" "--quiet"

  Write-Host "[4/9] 生成源码包"
  Invoke-Native powershell "-NoProfile" "-ExecutionPolicy" "Bypass" "-File" $PackageScript "-Version" $Version "-RepoRoot" $RepoRoot "-SkipImageBuild" "-SkipImageSave"

  Write-Host "[5/9] 串行构建并校验应用镜像: $($Services -join ', ')"
  foreach ($service in $Services) {
    if ($service -eq "task-worker") {
      Write-Host "预构建 task-worker builder 阶段，避免系统依赖与 LibreOffice 并行下载压垮代理"
      Invoke-Native docker "build" "--target" "builder" "--file" "docker/Dockerfile.worker" "--tag" "openrag-task-worker-builder-cache:$Version" "."
    }
    Invoke-Native docker "compose" "-p" "openrag" "--env-file" "docker/.env" "-f" "docker/docker-compose.prod.yml" "build" $service
  }
  $imageByService = @{
    "api" = "openrag-api"
    "web" = "openrag-web"
    "task-worker" = "openrag-task-worker"
  }
  $saveRefs = New-Object System.Collections.Generic.List[string]
  foreach ($service in $Services) {
    $image = $imageByService[$service]
    Invoke-Native docker "image" "inspect" $image
    Invoke-Native docker "tag" $image "${image}:$Version"
    [void]$saveRefs.Add("${image}:latest")
    [void]$saveRefs.Add("${image}:$Version")
    switch ($service) {
      "api" {
        Invoke-Native docker "run" "--rm" "${image}:$Version" "sh" "-c" "test -f /app/alembic.ini && test -d /app/alembic && python -m alembic --help >/dev/null"
        Invoke-Native docker "run" "--rm" "${image}:$Version" "python" "-c" "import openrag.api.main, numpy, pymilvus; print('api-import-ok')"
      }
      "web" {
        Invoke-Native docker "run" "--rm" "${image}:$Version" "nginx" "-t"
      }
      "task-worker" {
        Invoke-Native docker "run" "--rm" "${image}:$Version" "python" "-c" "import python_calamine, tiktoken; from openrag.parsers.adapters.excel_adapter import StructuredExcelParserAdapter; from openrag.chunking.excel_table_chunker import split_excel_blocks; print('worker-excel-import-ok')"
        Get-Content -LiteralPath (Join-Path $ServerScriptsDir "verify_excel_table_chunking.py") -Raw |
          docker run --rm -i "${image}:$Version" python -
        if ($LASTEXITCODE -ne 0) {
          throw "Worker Excel token 切分冒烟测试失败。"
        }
      }
    }
  }

  Write-Host "[6/9] 导出版本化镜像"
  New-Item -ItemType Directory -Force $BundleDir | Out-Null
  $imageTarName = "openrag-selected-images-$Version.tar"
  $imageTarPath = Join-Path $BundleDir $imageTarName
  $saveArgs = @("save", "-o", $imageTarPath) + @($saveRefs)
  Invoke-Native docker @saveArgs
  $imageHash = (Get-FileHash -LiteralPath $imageTarPath -Algorithm SHA256).Hash.ToLower()
  "$imageHash  $imageTarName" | Set-Content -LiteralPath "$imageTarPath.sha256" -Encoding ascii

  $thirdPartyIncluded = 0
  if ($IncludeThirdPartyImages) {
    $thirdPartyImages = @(
      "postgres:16-alpine",
      "quay.io/coreos/etcd:v3.5.5",
      "minio/minio:RELEASE.2023-03-20T20-16-18Z",
      "milvusdb/milvus:v2.4.17",
      "docker.elastic.co/elasticsearch/elasticsearch:8.12.2"
    )
    foreach ($image in $thirdPartyImages) {
      docker image inspect $image *> $null
      if ($LASTEXITCODE -ne 0) {
        Invoke-Native docker "pull" $image
      }
    }
    $thirdPartyTarName = "openrag-third-party-images.tar"
    $thirdPartyTarPath = Join-Path $BundleDir $thirdPartyTarName
    Invoke-Native docker "save" "-o" $thirdPartyTarPath @thirdPartyImages
    $thirdPartyHash = (Get-FileHash -LiteralPath $thirdPartyTarPath -Algorithm SHA256).Hash.ToLower()
    "$thirdPartyHash  $thirdPartyTarName" | Set-Content -LiteralPath "$thirdPartyTarPath.sha256" -Encoding ascii
    $thirdPartyIncluded = 1
  }

  Write-Host "[7/9] 组装离线目录"
  $sourceNames = @(
    "openrag-$Version-src.zip",
    "openrag-$Version-src.zip.sha256",
    "openrag-$Version-manifest.json"
  )
  foreach ($name in $sourceNames) {
    $source = Join-Path $ArtifactsDir $name
    Require-File $source
    Copy-Item -LiteralPath $source -Destination (Join-Path $BundleDir $name)
  }
  Copy-Item -LiteralPath $ServerScriptsDir -Destination (Join-Path $BundleDir "scripts") -Recurse
  Copy-Item -LiteralPath $GuidePath -Destination (Join-Path $BundleDir "README.md")
  Get-ChildItem -LiteralPath (Join-Path $BundleDir "scripts") -Filter "*.sh" -File |
    ForEach-Object {
      $content = [System.IO.File]::ReadAllText($_.FullName).Replace("`r`n", "`n")
      [System.IO.File]::WriteAllText(
        $_.FullName,
        $content,
        [System.Text.UTF8Encoding]::new($false)
      )
    }

  $commit = (& git rev-parse HEAD).Trim()
  $baseCommit = (& git rev-parse $BaseRef).Trim()
  $changedFiles = @(& git diff --name-only "$BaseRef..HEAD")
  $requiresMigrationValue = if ($RequiresMigration) { 1 } else { 0 }
  $selectedServicesText = $Services -join " "
  $releaseEnvLines = @(
    "DEPLOY_VERSION='$Version'",
    "GIT_COMMIT='$commit'",
    "BASE_COMMIT='$baseCommit'",
    "SELECTED_SERVICES='$selectedServicesText'",
    "REQUIRES_MIGRATION='$requiresMigrationValue'",
    "THIRD_PARTY_INCLUDED='$thirdPartyIncluded'"
  )
  [System.IO.File]::WriteAllText(
    (Join-Path $BundleDir "release.env"),
    ($releaseEnvLines -join "`n") + "`n",
    [System.Text.UTF8Encoding]::new($false)
  )

  $manifest = [ordered]@{
    schema_version = "openrag-offline-compose-v1"
    version = $Version
    git_commit = $commit
    base_ref = $BaseRef
    base_commit = $baseCommit
    built_at = (Get-Date).ToString("o")
    selected_services = @($Services)
    selected_images = @($Services | ForEach-Object { "{0}:{1}" -f $imageByService[$_], $Version })
    requires_migration = [bool]$RequiresMigration
    third_party_images_included = [bool]$IncludeThirdPartyImages
    runtime_env_included = $false
    changed_files = @($changedFiles)
    safety = [ordered]@{
      server_build = false
      server_pull = false
      preserve_shared_env = true
      preserve_data_override = true
      automatic_app_rollback = (-not [bool]$RequiresMigration)
    }
  }
  $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $BundleDir "openrag-offline-manifest.json") -Encoding utf8

  $planLines = @(
    "# OpenRag 内网离线部署计划",
    "",
    "- Version: $Version",
    "- Git commit: $commit",
    "- Base: $BaseRef ($baseCommit)",
    "- Selected services: $($Services -join ', ')",
    "- Requires migration: $([bool]$RequiresMigration)",
    "- Third-party images included: $([bool]$IncludeThirdPartyImages)",
    "- Runtime env included: false",
    "",
    "## Changed files",
    ""
  ) + @($changedFiles | ForEach-Object { "- $_" }) + @(
    "",
    "## Risk and rollback",
    "",
    "- 内网只执行 sha256、docker load 和 docker compose up --no-build。",
    "- shared/openrag.env 和 docker-compose.data-active.yml 不进入包且不会被覆盖。",
    "- prepare 阶段保存当前容器镜像 ID、current 指针和辅助文件。",
    "- 验证失败时，无数据库迁移的发布自动执行应用回退。",
    "- 包含数据库迁移时禁止自动假定 schema 可降级。"
  )
  [System.IO.File]::WriteAllText(
    (Join-Path $BundleDir "deploy-plan-$Version.md"),
    ($planLines -join "`n") + "`n",
    [System.Text.UTF8Encoding]::new($false)
  )

  Write-Host "[8/9] 检查离线包不包含运行时密钥"
  $secretMatches = Get-ChildItem -LiteralPath $BundleDir -Recurse -File |
    Where-Object { $_.Name -in @(".env", "openrag.env") }
  if ($secretMatches) {
    throw "离线包包含禁止的运行时环境文件: $($secretMatches.FullName -join ', ')"
  }

  Write-Host "[9/9] 生成单文件搬运包和 sha256"
  Invoke-Native tar.exe "-cf" $OuterTar "-C" $ArtifactsDir $BundleName
  $outerHash = (Get-FileHash -LiteralPath $OuterTar -Algorithm SHA256).Hash.ToLower()
  "$outerHash  $BundleName.tar" | Set-Content -LiteralPath "$OuterTar.sha256" -Encoding ascii

  Write-Host "offline-release-build-ok"
  Get-Item -LiteralPath $OuterTar, "$OuterTar.sha256" | Select-Object Length, FullName
}
finally {
  Pop-Location
}
