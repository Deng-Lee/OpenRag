# Internal MinIO Render Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a one-command PowerShell workflow that renders internal-network Kubernetes manifests from the existing `k8s/` baseline by applying a non-secret MinIO profile and a deployment image tag.

**Architecture:** Keep `k8s/` as the external-network baseline, add `deploy/envs/internal.yaml` as the internal MinIO profile, and generate `k8s-rendered/internal/` as disposable output. The renderer copies root Kubernetes YAML files, excludes real Secret files, patches only MinIO fields and local image tags, then validates the output with deterministic string checks plus optional `kubectl kustomize`.

**Tech Stack:** PowerShell script, simple repository-local YAML profile parser for the fixed `minio:` mapping, Kubernetes YAML manifests, optional `kubectl kustomize`, Git.

---

## File Structure

- Create `deploy/envs/internal.yaml`
  - Holds only non-sensitive internal MinIO parameters.
  - No access key or secret key.

- Create `scripts/render-k8s-profile.ps1`
  - Reads `deploy/envs/<Profile>.yaml`.
  - Prompts for `-Tag` when not supplied.
  - Copies root `k8s/*.yaml` files to `k8s-rendered/<Profile>/`, excluding `01-secret.yaml`.
  - Patches MinIO fields in API, worker, Milvus, and `service_conf.yaml`.
  - Patches local OpenRag image tags without introducing a registry.
  - Forces `imagePullPolicy: IfNotPresent` for generated business manifests.
  - Validates rendered output and optionally runs `kubectl kustomize`.

- Create `scripts/test-render-k8s-profile.ps1`
  - Runs the renderer against the real `k8s/` baseline into `.tmp-rendered-test/`.
  - Asserts rendered MinIO values, image tags, secret placeholders, excluded `01-secret.yaml`, and generated kustomize viability when `kubectl` is present.
  - Cleans its temporary output.

- Modify `.gitignore`
  - Ignore `k8s-rendered/` and `.tmp-rendered-test/`.

- Keep `config.md`
  - No implementation changes required in this task unless the renderer behavior intentionally differs from the already approved reference.

---

### Task 1: Add Internal MinIO Profile And Ignore Generated Output

**Files:**
- Create: `deploy/envs/internal.yaml`
- Modify: `.gitignore`

- [ ] **Step 1: Create the profile directory and internal profile**

Create `deploy/envs/internal.yaml` with exactly:

```yaml
minio:
  endpoint: http://172.16.31.63:9000
  address: 172.16.31.63
  port: 9000
  storageBucket: rag-kb
  storagePrefix: openrag
  publicUrl: http://172.16.31.63:9000
  milvusBucket: rag-kb
  milvusRootPath: milvus
```

- [ ] **Step 2: Ignore generated render directories**

Append these lines to `.gitignore` under the existing generated/offline artifact section:

```gitignore
# Generated Kubernetes render output
k8s-rendered/
.tmp-rendered-test/
```

- [ ] **Step 3: Verify the profile has no credentials**

Run:

```powershell
Select-String -Path deploy\envs\internal.yaml -Pattern 'access|secret|password|key' -CaseSensitive
```

Expected: no output.

- [ ] **Step 4: Commit**

```powershell
git add .gitignore deploy/envs/internal.yaml
git commit -m "chore: add internal minio render profile"
```

---

### Task 2: Write The Renderer Test Harness First

**Files:**
- Create: `scripts/test-render-k8s-profile.ps1`

- [ ] **Step 1: Create the scripts directory and failing test harness**

Create `scripts/test-render-k8s-profile.ps1` with exactly:

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Renderer = Join-Path $PSScriptRoot "render-k8s-profile.ps1"
$OutputRoot = Join-Path $RepoRoot ".tmp-rendered-test"
$RenderedDir = Join-Path $OutputRoot "internal"

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Assert-FileContains {
    param(
        [string]$Path,
        [string]$Needle
    )
    $text = Get-Content -Path $Path -Raw -Encoding UTF8
    Assert-True -Condition $text.Contains($Needle) -Message "Expected '$Path' to contain '$Needle'."
}

function Assert-FileNotContains {
    param(
        [string]$Path,
        [string]$Needle
    )
    $text = Get-Content -Path $Path -Raw -Encoding UTF8
    Assert-True -Condition (-not $text.Contains($Needle)) -Message "Expected '$Path' to not contain '$Needle'."
}

function Remove-TestOutput {
    if (Test-Path -LiteralPath $OutputRoot) {
        $repoFull = [System.IO.Path]::GetFullPath($RepoRoot)
        $targetFull = [System.IO.Path]::GetFullPath($OutputRoot)
        Assert-True -Condition $targetFull.StartsWith($repoFull, [System.StringComparison]::OrdinalIgnoreCase) -Message "Refusing to remove path outside repo: $targetFull"
        Remove-Item -LiteralPath $OutputRoot -Recurse -Force
    }
}

try {
    Remove-TestOutput

    & $Renderer -Profile internal -Tag 9.9.9 -OutputRoot $OutputRoot -SkipKustomize

    Assert-True -Condition (Test-Path -LiteralPath $RenderedDir) -Message "Rendered directory was not created."
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $RenderedDir "kustomization.yaml")) -Message "kustomization.yaml was not rendered."
    Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $RenderedDir "01-secret.yaml"))) -Message "01-secret.yaml must not be copied into rendered output."

    Assert-FileContains -Path (Join-Path $RenderedDir "09-api.yaml") -Needle "value: http://172.16.31.63:9000"
    Assert-FileContains -Path (Join-Path $RenderedDir "09-api.yaml") -Needle "value: rag-kb"
    Assert-FileContains -Path (Join-Path $RenderedDir "09-api.yaml") -Needle "value: openrag"
    Assert-FileContains -Path (Join-Path $RenderedDir "09-api.yaml") -Needle "image: openrag/api:9.9.9"
    Assert-FileContains -Path (Join-Path $RenderedDir "09-api.yaml") -Needle "imagePullPolicy: IfNotPresent"

    Assert-FileContains -Path (Join-Path $RenderedDir "10-task-worker.yaml") -Needle "image: openrag/task-worker:9.9.9"
    Assert-FileContains -Path (Join-Path $RenderedDir "10-task-worker.yaml") -Needle "imagePullPolicy: IfNotPresent"

    Assert-FileContains -Path (Join-Path $RenderedDir "11-web.yaml") -Needle "image: openrag/web:9.9.9"
    Assert-FileContains -Path (Join-Path $RenderedDir "11-web.yaml") -Needle "imagePullPolicy: IfNotPresent"
    Assert-FileNotContains -Path (Join-Path $RenderedDir "11-web.yaml") -Needle "imagePullPolicy: Always"

    Assert-FileContains -Path (Join-Path $RenderedDir "07-milvus.yaml") -Needle "value: 172.16.31.63:9000"
    Assert-FileContains -Path (Join-Path $RenderedDir "07-milvus-config.yaml") -Needle "address: 172.16.31.63"
    Assert-FileContains -Path (Join-Path $RenderedDir "07-milvus-config.yaml") -Needle "port: 9000"
    Assert-FileContains -Path (Join-Path $RenderedDir "07-milvus-config.yaml") -Needle "bucketName: rag-kb"
    Assert-FileContains -Path (Join-Path $RenderedDir "07-milvus-config.yaml") -Needle "rootPath: milvus"
    Assert-FileContains -Path (Join-Path $RenderedDir "07-milvus-config.yaml") -Needle "secretAccessKey: CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"

    Assert-FileContains -Path (Join-Path $RenderedDir "15-configmap-openrag-service-conf.yaml") -Needle "host: 172.16.31.63:9000"
    Assert-FileContains -Path (Join-Path $RenderedDir "15-configmap-openrag-service-conf.yaml") -Needle "bucket: rag-kb"
    Assert-FileContains -Path (Join-Path $RenderedDir "15-configmap-openrag-service-conf.yaml") -Needle "prefix_path: openrag"
    Assert-FileContains -Path (Join-Path $RenderedDir "15-configmap-openrag-service-conf.yaml") -Needle "password: CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"

    Assert-FileContains -Path (Join-Path $RenderedDir "01-secret.example.yaml") -Needle 'MINIO_ROOT_USER: "CHANGE_ME_INTERNAL_MINIO_ACCESS_KEY"'
    Assert-FileContains -Path (Join-Path $RenderedDir "01-secret.example.yaml") -Needle 'MINIO_ROOT_PASSWORD: "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"'
    Assert-FileContains -Path (Join-Path $RenderedDir "01-secret.example.yaml") -Needle 'MILVUS_SECRET_KEY: "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"'

    $kubectl = Get-Command kubectl -ErrorAction SilentlyContinue
    if ($kubectl) {
        kubectl kustomize $RenderedDir | Out-Null
    } else {
        Write-Host "kubectl not found; skipped kustomize smoke test."
    }

    Write-Host "render-k8s-profile tests passed"
}
finally {
    Remove-TestOutput
}
```

- [ ] **Step 2: Run the test and verify it fails because the renderer does not exist**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-render-k8s-profile.ps1
```

Expected: FAIL with an error that includes `render-k8s-profile.ps1` because the renderer has not been created yet.

- [ ] **Step 3: Commit the failing test harness**

```powershell
git add scripts/test-render-k8s-profile.ps1
git commit -m "test: add k8s render profile harness"
```

---

### Task 3: Implement The Renderer

**Files:**
- Create: `scripts/render-k8s-profile.ps1`

- [ ] **Step 1: Create the renderer**

Create `scripts/render-k8s-profile.ps1` with exactly:

```powershell
param(
    [string]$Profile = "internal",
    [string]$Tag,
    [string]$ProfilePath,
    [string]$SourceDir,
    [string]$OutputRoot,
    [switch]$SkipKustomize
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $SourceDir) {
    $SourceDir = Join-Path $RepoRoot "k8s"
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $RepoRoot "k8s-rendered"
}
if (-not $ProfilePath) {
    $ProfilePath = Join-Path $RepoRoot "deploy\envs\$Profile.yaml"
}

function Assert-PathInside {
    param(
        [string]$BasePath,
        [string]$TargetPath,
        [string]$Purpose
    )
    $baseFull = [System.IO.Path]::GetFullPath($BasePath)
    $targetFull = [System.IO.Path]::GetFullPath($TargetPath)
    if (-not $targetFull.StartsWith($baseFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to use $Purpose outside '$baseFull': $targetFull"
    }
}

function Read-MinioProfile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Profile file not found: $Path"
    }

    $values = @{}
    $inMinio = $false
    foreach ($rawLine in Get-Content -Path $Path -Encoding UTF8) {
        $line = $rawLine
        $hashIndex = $line.IndexOf("#")
        if ($hashIndex -ge 0) {
            $line = $line.Substring(0, $hashIndex)
        }
        $trimmed = $line.TrimEnd()
        if ($trimmed -match "^\s*$") {
            continue
        }
        if ($trimmed -match "^minio:\s*$") {
            $inMinio = $true
            continue
        }
        if ($inMinio -and $trimmed -match "^\S") {
            $inMinio = $false
        }
        if ($inMinio -and $trimmed -match "^\s{2}([A-Za-z][A-Za-z0-9]*)\s*:\s*(.*?)\s*$") {
            $key = $Matches[1]
            $value = $Matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$key] = $value
        }
    }

    $required = @(
        "endpoint",
        "address",
        "port",
        "storageBucket",
        "storagePrefix",
        "publicUrl",
        "milvusBucket",
        "milvusRootPath"
    )
    foreach ($key in $required) {
        if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace([string]$values[$key])) {
            throw "Profile '$Path' is missing minio.$key"
        }
    }
    if ($values["endpoint"] -match "(?i)access|secret|password") {
        throw "minio.endpoint appears to contain a credential-like value."
    }

    [pscustomobject]@{
        Endpoint       = [string]$values["endpoint"]
        Address        = [string]$values["address"]
        Port           = [string]$values["port"]
        StorageBucket  = [string]$values["storageBucket"]
        StoragePrefix  = [string]$values["storagePrefix"]
        PublicUrl      = [string]$values["publicUrl"]
        MilvusBucket   = [string]$values["milvusBucket"]
        MilvusRootPath = [string]$values["milvusRootPath"]
    }
}

function Read-Text {
    param([string]$Path)
    Get-Content -Path $Path -Raw -Encoding UTF8
}

function Write-Text {
    param(
        [string]$Path,
        [string]$Text
    )
    [System.IO.File]::WriteAllText($Path, $Text, [System.Text.UTF8Encoding]::new($false))
}

function Replace-ExactlyOne {
    param(
        [string]$Text,
        [string]$Pattern,
        [string]$Replacement,
        [string]$Description
    )
    $matches = [regex]::Matches($Text, $Pattern)
    if ($matches.Count -ne 1) {
        throw "Expected exactly one match for $Description, found $($matches.Count)."
    }
    [regex]::Replace($Text, $Pattern, $Replacement, 1)
}

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Value
    )
    $text = Read-Text $Path
    $namePattern = [regex]::Escape($Name)
    $pattern = "(?m)(- name:\s+$namePattern\s*\r?\n\s+value:\s*).*$"
    $text = Replace-ExactlyOne -Text $text -Pattern $pattern -Replacement "`${1}$Value" -Description "$Path env $Name"
    Write-Text -Path $Path -Text $text
}

function Set-ScalarLine {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )
    $text = Read-Text $Path
    $keyPattern = [regex]::Escape($Key)
    $pattern = "(?m)^(\s+${keyPattern}:\s*).*$"
    $text = Replace-ExactlyOne -Text $text -Pattern $pattern -Replacement "`${1}$Value" -Description "$Path scalar $Key"
    Write-Text -Path $Path -Text $text
}

function Set-QuotedScalarLine {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )
    $text = Read-Text $Path
    $keyPattern = [regex]::Escape($Key)
    $pattern = "(?m)^(\s+${keyPattern}:\s*).*$"
    $text = Replace-ExactlyOne -Text $text -Pattern $pattern -Replacement "`${1}`"$Value`"" -Description "$Path scalar $Key"
    Write-Text -Path $Path -Text $text
}

function Set-ImageTag {
    param(
        [string]$Path,
        [string]$ImageName,
        [string]$Tag
    )
    $text = Read-Text $Path
    $imagePattern = [regex]::Escape($ImageName)
    $pattern = "(?m)^(\s*image:\s+${imagePattern}:)[^\s]+$"
    $text = Replace-ExactlyOne -Text $text -Pattern $pattern -Replacement "`${1}$Tag" -Description "$Path image $ImageName"
    Write-Text -Path $Path -Text $text
}

function Set-IfNotPresentPolicy {
    param([string]$Path)
    $text = Read-Text $Path
    $text = [regex]::Replace($text, "(?m)^(\s*imagePullPolicy:\s*).*$", "`${1}IfNotPresent")
    Write-Text -Path $Path -Text $text
}

function Copy-K8sRootYaml {
    param(
        [string]$From,
        [string]$To
    )
    if (-not (Test-Path -LiteralPath $From)) {
        throw "Source k8s directory not found: $From"
    }
    if (Test-Path -LiteralPath $To) {
        Assert-PathInside -BasePath $RepoRoot -TargetPath $To -Purpose "render output"
        Remove-Item -LiteralPath $To -Recurse -Force
    }
    New-Item -ItemType Directory -Path $To -Force | Out-Null
    Get-ChildItem -Path $From -File -Filter "*.yaml" | Where-Object {
        $_.Name -ne "01-secret.yaml"
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $To $_.Name) -Force
    }
}

function Assert-Contains {
    param(
        [string]$Path,
        [string]$Needle
    )
    $text = Read-Text $Path
    if (-not $text.Contains($Needle)) {
        throw "Validation failed: '$Path' does not contain '$Needle'."
    }
}

function Assert-NotContains {
    param(
        [string]$Path,
        [string]$Needle
    )
    $text = Read-Text $Path
    if ($text.Contains($Needle)) {
        throw "Validation failed: '$Path' still contains forbidden text '$Needle'."
    }
}

function Assert-RenderedOutput {
    param(
        [string]$Dir,
        [object]$Minio,
        [string]$ImageTag
    )
    $requiredFiles = @(
        "01-secret.example.yaml",
        "07-milvus.yaml",
        "07-milvus-config.yaml",
        "09-api.yaml",
        "10-task-worker.yaml",
        "11-web.yaml",
        "15-configmap-openrag-service-conf.yaml",
        "kustomization.yaml"
    )
    foreach ($file in $requiredFiles) {
        $path = Join-Path $Dir $file
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Rendered file missing: $path"
        }
    }
    if (Test-Path -LiteralPath (Join-Path $Dir "01-secret.yaml")) {
        throw "Rendered output must not include 01-secret.yaml."
    }

    Assert-Contains -Path (Join-Path $Dir "09-api.yaml") -Needle "value: $($Minio.Endpoint)"
    Assert-Contains -Path (Join-Path $Dir "09-api.yaml") -Needle "value: $($Minio.StorageBucket)"
    Assert-Contains -Path (Join-Path $Dir "09-api.yaml") -Needle "value: $($Minio.StoragePrefix)"
    Assert-Contains -Path (Join-Path $Dir "09-api.yaml") -Needle "value: $($Minio.PublicUrl)"
    Assert-Contains -Path (Join-Path $Dir "09-api.yaml") -Needle "image: openrag/api:$ImageTag"

    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.Endpoint)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.StorageBucket)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.StoragePrefix)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.PublicUrl)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "image: openrag/task-worker:$ImageTag"

    Assert-Contains -Path (Join-Path $Dir "11-web.yaml") -Needle "image: openrag/web:$ImageTag"
    Assert-NotContains -Path (Join-Path $Dir "11-web.yaml") -Needle "imagePullPolicy: Always"

    Assert-Contains -Path (Join-Path $Dir "07-milvus.yaml") -Needle "value: $($Minio.Address):$($Minio.Port)"
    Assert-Contains -Path (Join-Path $Dir "07-milvus-config.yaml") -Needle "address: $($Minio.Address)"
    Assert-Contains -Path (Join-Path $Dir "07-milvus-config.yaml") -Needle "port: $($Minio.Port)"
    Assert-Contains -Path (Join-Path $Dir "07-milvus-config.yaml") -Needle "bucketName: $($Minio.MilvusBucket)"
    Assert-Contains -Path (Join-Path $Dir "07-milvus-config.yaml") -Needle "rootPath: $($Minio.MilvusRootPath)"
    Assert-Contains -Path (Join-Path $Dir "07-milvus-config.yaml") -Needle "secretAccessKey: CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"

    Assert-Contains -Path (Join-Path $Dir "15-configmap-openrag-service-conf.yaml") -Needle "host: $($Minio.Address):$($Minio.Port)"
    Assert-Contains -Path (Join-Path $Dir "15-configmap-openrag-service-conf.yaml") -Needle "bucket: $($Minio.StorageBucket)"
    Assert-Contains -Path (Join-Path $Dir "15-configmap-openrag-service-conf.yaml") -Needle "prefix_path: $($Minio.StoragePrefix)"
    Assert-Contains -Path (Join-Path $Dir "15-configmap-openrag-service-conf.yaml") -Needle "password: CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"

    Assert-Contains -Path (Join-Path $Dir "01-secret.example.yaml") -Needle 'MINIO_ROOT_USER: "CHANGE_ME_INTERNAL_MINIO_ACCESS_KEY"'
    Assert-Contains -Path (Join-Path $Dir "01-secret.example.yaml") -Needle 'MINIO_ROOT_PASSWORD: "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"'
    Assert-Contains -Path (Join-Path $Dir "01-secret.example.yaml") -Needle 'MILVUS_SECRET_KEY: "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"'

    Get-ChildItem -Path $Dir -File -Filter "*.yaml" | ForEach-Object {
        Assert-NotContains -Path $_.FullName -Needle "CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY"
    }
}

if ([string]::IsNullOrWhiteSpace($Tag)) {
    $Tag = Read-Host "请输入待部署版本号，例如 1.1.1"
}
if ([string]::IsNullOrWhiteSpace($Tag)) {
    throw "Image tag is required."
}
if ($Tag -match "\s") {
    throw "Image tag must not contain whitespace: '$Tag'"
}

$SourceDir = [System.IO.Path]::GetFullPath($SourceDir)
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$ProfilePath = [System.IO.Path]::GetFullPath($ProfilePath)
$OutputDir = Join-Path $OutputRoot $Profile

Assert-PathInside -BasePath $RepoRoot -TargetPath $OutputRoot -Purpose "render output root"
Assert-PathInside -BasePath $RepoRoot -TargetPath $OutputDir -Purpose "render output directory"

$minio = Read-MinioProfile -Path $ProfilePath

Copy-K8sRootYaml -From $SourceDir -To $OutputDir

$apiYaml = Join-Path $OutputDir "09-api.yaml"
$workerYaml = Join-Path $OutputDir "10-task-worker.yaml"
$webYaml = Join-Path $OutputDir "11-web.yaml"
$milvusYaml = Join-Path $OutputDir "07-milvus.yaml"
$milvusConfigYaml = Join-Path $OutputDir "07-milvus-config.yaml"
$serviceConfYaml = Join-Path $OutputDir "15-configmap-openrag-service-conf.yaml"
$secretExampleYaml = Join-Path $OutputDir "01-secret.example.yaml"

foreach ($path in @($apiYaml, $workerYaml)) {
    Set-EnvValue -Path $path -Name "STORAGE_ENDPOINT" -Value $minio.Endpoint
    Set-EnvValue -Path $path -Name "STORAGE_BUCKET" -Value $minio.StorageBucket
    Set-EnvValue -Path $path -Name "STORAGE_PREFIX" -Value $minio.StoragePrefix
    Set-EnvValue -Path $path -Name "STORAGE_PUBLIC_URL" -Value $minio.PublicUrl
    Set-IfNotPresentPolicy -Path $path
}

Set-ImageTag -Path $apiYaml -ImageName "openrag/api" -Tag $Tag
Set-ImageTag -Path $workerYaml -ImageName "openrag/task-worker" -Tag $Tag
Set-ImageTag -Path $webYaml -ImageName "openrag/web" -Tag $Tag
Set-IfNotPresentPolicy -Path $webYaml

Set-EnvValue -Path $milvusYaml -Name "MINIO_ADDRESS" -Value "$($minio.Address):$($minio.Port)"
Set-ScalarLine -Path $milvusConfigYaml -Key "address" -Value $minio.Address
Set-ScalarLine -Path $milvusConfigYaml -Key "port" -Value $minio.Port
Set-ScalarLine -Path $milvusConfigYaml -Key "secretAccessKey" -Value "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"
Set-ScalarLine -Path $milvusConfigYaml -Key "bucketName" -Value $minio.MilvusBucket
Set-ScalarLine -Path $milvusConfigYaml -Key "rootPath" -Value $minio.MilvusRootPath

Set-ScalarLine -Path $serviceConfYaml -Key "host" -Value "$($minio.Address):$($minio.Port)"
Set-ScalarLine -Path $serviceConfYaml -Key "user" -Value "CHANGE_ME_INTERNAL_MINIO_ACCESS_KEY"
Set-ScalarLine -Path $serviceConfYaml -Key "password" -Value "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"
Set-ScalarLine -Path $serviceConfYaml -Key "bucket" -Value $minio.StorageBucket
Set-ScalarLine -Path $serviceConfYaml -Key "prefix_path" -Value $minio.StoragePrefix

Set-QuotedScalarLine -Path $secretExampleYaml -Key "MINIO_ROOT_USER" -Value "CHANGE_ME_INTERNAL_MINIO_ACCESS_KEY"
Set-QuotedScalarLine -Path $secretExampleYaml -Key "MINIO_ROOT_PASSWORD" -Value "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"
Set-QuotedScalarLine -Path $secretExampleYaml -Key "MILVUS_SECRET_KEY" -Value "CHANGE_ME_INTERNAL_MINIO_SECRET_KEY"

Assert-RenderedOutput -Dir $OutputDir -Minio $minio -ImageTag $Tag

if (-not $SkipKustomize) {
    $kubectl = Get-Command kubectl -ErrorAction SilentlyContinue
    if ($kubectl) {
        kubectl kustomize $OutputDir | Out-Null
    } else {
        Write-Host "kubectl not found; skipped kustomize validation."
    }
}

Write-Host "Rendered profile '$Profile' to $OutputDir with OpenRag image tag '$Tag'."
```

- [ ] **Step 2: Run the renderer test harness and verify it passes**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-render-k8s-profile.ps1
```

Expected: output includes `render-k8s-profile tests passed`. If `kubectl` is unavailable, output also includes `kubectl not found; skipped kustomize smoke test.`

- [ ] **Step 3: Run the renderer manually into the real generated directory**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\render-k8s-profile.ps1 -Profile internal -Tag 1.1.1 -SkipKustomize
```

Expected: output includes `Rendered profile 'internal' to` and `OpenRag image tag '1.1.1'`.

- [ ] **Step 4: Inspect generated output**

Run:

```powershell
Select-String -Path k8s-rendered\internal\*.yaml -Pattern 'openrag/api:1.1.1|openrag/task-worker:1.1.1|openrag/web:1.1.1|STORAGE_ENDPOINT|STORAGE_BUCKET|STORAGE_PREFIX|MINIO_ADDRESS|CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY|imagePullPolicy: Always'
```

Expected:
- Lines for the three `openrag/*:1.1.1` images.
- Lines for expected storage and MinIO variable names.
- No line containing `CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY`.
- No line containing `imagePullPolicy: Always`.

- [ ] **Step 5: Remove generated output after manual inspection**

Run:

```powershell
Remove-Item -LiteralPath k8s-rendered\internal -Recurse -Force
```

Expected: `k8s-rendered\internal` is removed. This is generated output and is ignored by Git.

- [ ] **Step 6: Commit the renderer**

```powershell
git add scripts/render-k8s-profile.ps1
git commit -m "feat: render internal minio k8s profile"
```

---

### Task 4: Verify Prompted Tag Mode

**Files:**
- No file changes expected.

- [ ] **Step 1: Run the renderer without `-Tag` and provide input**

Run:

```powershell
"2.3.4" | powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\render-k8s-profile.ps1 -Profile internal -OutputRoot .tmp-rendered-test -SkipKustomize
```

Expected:
- Output includes `Rendered profile 'internal'`.
- Output includes `OpenRag image tag '2.3.4'`.

- [ ] **Step 2: Check prompted tag was applied**

Run:

```powershell
Select-String -Path .tmp-rendered-test\internal\09-api.yaml,.tmp-rendered-test\internal\10-task-worker.yaml,.tmp-rendered-test\internal\11-web.yaml -Pattern 'openrag/api:2.3.4|openrag/task-worker:2.3.4|openrag/web:2.3.4'
```

Expected: one line for each image:

```text
openrag/api:2.3.4
openrag/task-worker:2.3.4
openrag/web:2.3.4
```

- [ ] **Step 3: Remove prompted-mode test output**

Run:

```powershell
Remove-Item -LiteralPath .tmp-rendered-test -Recurse -Force
```

Expected: `.tmp-rendered-test` is removed.

- [ ] **Step 4: Commit if no changes were required**

Run:

```powershell
git status --short
```

Expected: no tracked changes from Task 4.

If Task 4 required a renderer fix, commit only that fix:

```powershell
git add scripts/render-k8s-profile.ps1
git commit -m "fix: support prompted render tag"
```

---

### Task 5: Final End-To-End Verification And Documentation Cross-Check

**Files:**
- Read: `config.md`
- Read: `deploy/envs/internal.yaml`
- Read: `scripts/render-k8s-profile.ps1`
- Read: `scripts/test-render-k8s-profile.ps1`
- Read: `.gitignore`

- [ ] **Step 1: Run the renderer tests**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-render-k8s-profile.ps1
```

Expected: output includes `render-k8s-profile tests passed`.

- [ ] **Step 2: Render the real internal output**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\render-k8s-profile.ps1 -Profile internal -Tag 1.1.1
```

Expected:
- Output includes `Rendered profile 'internal'`.
- If `kubectl` exists, the command exits successfully after `kubectl kustomize`.
- If `kubectl` does not exist, output includes `kubectl not found; skipped kustomize validation.`

- [ ] **Step 3: Validate generated files match `config.md`**

Run:

```powershell
Select-String -Path k8s-rendered\internal\*.yaml -Pattern 'http://172.16.31.63:9000|172.16.31.63:9000|rag-kb|openrag|rootPath: milvus|openrag/api:1.1.1|openrag/task-worker:1.1.1|openrag/web:1.1.1'
```

Expected: matches appear in API, worker, Milvus, service conf, and business image manifests.

- [ ] **Step 4: Validate forbidden generated content is absent**

Run:

```powershell
Select-String -Path k8s-rendered\internal\*.yaml -Pattern 'CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY|imagePullPolicy: Always|YOUR_REGISTRY'
```

Expected: no output.

- [ ] **Step 5: Confirm generated output is ignored**

Run:

```powershell
git status --short k8s-rendered .tmp-rendered-test
```

Expected: no output.

- [ ] **Step 6: Remove generated output**

Run:

```powershell
Remove-Item -LiteralPath k8s-rendered\internal -Recurse -Force
```

Expected: `k8s-rendered\internal` is removed.

- [ ] **Step 7: Review tracked changes**

Run:

```powershell
git status --short
```

Expected: no uncommitted changes from this plan after all commits are made. Unrelated pre-existing changes may still appear; do not revert them.

---

## Self-Review

- Spec coverage: The plan covers the approved `config.md` requirements: profile in `deploy/envs/internal.yaml`, one-command PowerShell rendering, `-Tag` support and prompt fallback, no registry handling, local image tag updates, single bucket MinIO mapping, placeholder-only secrets, generated output ignored, validation checks, and optional `kubectl kustomize`.
- Placeholder scan: This plan intentionally contains no unresolved placeholders or vague implementation steps. Every file creation task includes exact content or exact commands.
- Type consistency: The profile keys used in `internal.yaml` match the renderer property names: `endpoint`, `address`, `port`, `storageBucket`, `storagePrefix`, `publicUrl`, `milvusBucket`, and `milvusRootPath`.
