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

function Assert-PathInside {
    param(
        [string]$BasePath,
        [string]$TargetPath,
        [string]$Purpose
    )
    $baseFull = [System.IO.Path]::GetFullPath($BasePath)
    $targetFull = [System.IO.Path]::GetFullPath($TargetPath)
    $baseTrimmed = $baseFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $targetTrimmed = $targetFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $baseWithSeparator = $baseTrimmed + [System.IO.Path]::DirectorySeparatorChar
    $inside = $targetTrimmed.Equals($baseTrimmed, [System.StringComparison]::OrdinalIgnoreCase) -or
        $targetFull.StartsWith($baseWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)
    if (-not $inside) {
        throw "Refusing to use $Purpose outside '$baseFull': $targetFull"
    }
}

function Assert-PathNotEqual {
    param(
        [string]$LeftPath,
        [string]$RightPath,
        [string]$Message
    )
    $leftFull = [System.IO.Path]::GetFullPath($LeftPath).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $rightFull = [System.IO.Path]::GetFullPath($RightPath).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    if ($leftFull.Equals($rightFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw $Message
    }
}

function Validate-ProfileName {
    param([string]$Name)

    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw "Profile name is required."
    }
    if ($Name -eq "." -or $Name -eq ".." -or $Name -notmatch "^[A-Za-z0-9._-]+$") {
        throw "Invalid profile name '$Name'. Use only letters, numbers, dot, underscore, and hyphen; '.' and '..' are not allowed."
    }
}

function Validate-OutputRoot {
    param(
        [string]$Path,
        [string]$SourcePath
    )

    $allowedNames = @("k8s-rendered", ".tmp-rendered-test")
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $sourceFull = [System.IO.Path]::GetFullPath($SourcePath)
    $name = Split-Path -Path $fullPath -Leaf

    if (-not $allowedNames.Contains($name)) {
        throw "Invalid output root '$fullPath'. OutputRoot must be named 'k8s-rendered' or '.tmp-rendered-test'."
    }
    Assert-PathInside -BasePath $RepoRoot -TargetPath $fullPath -Purpose "render output root"
    Assert-PathNotEqual -LeftPath $fullPath -RightPath $RepoRoot -Message "OutputRoot must not be the repository root: $fullPath"
    Assert-PathNotEqual -LeftPath $fullPath -RightPath $sourceFull -Message "OutputRoot must not be the source k8s directory: $fullPath"
    if ($fullPath.StartsWith($sourceFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OutputRoot must not be inside the source k8s directory: $fullPath"
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
    $pattern = "(?m)^(\s*image:\s+${imagePattern}:)[^\r\n]+"
    $text = Replace-ExactlyOne -Text $text -Pattern $pattern -Replacement "`${1}$Tag" -Description "$Path image $ImageName"
    Write-Text -Path $Path -Text $text
}

function Set-IfNotPresentPolicy {
    param([string]$Path)
    $text = Read-Text $Path
    $text = Replace-ExactlyOne -Text $text -Pattern "(?m)^(\s*imagePullPolicy:\s*).*$" -Replacement "`${1}IfNotPresent" -Description "$Path imagePullPolicy"
    Write-Text -Path $Path -Text $text
}

function Get-KustomizationResources {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "kustomization.yaml not found: $Path"
    }

    $resources = @()
    $inResources = $false
    foreach ($rawLine in Get-Content -Path $Path -Encoding UTF8) {
        $line = $rawLine
        $hashIndex = $line.IndexOf("#")
        if ($hashIndex -ge 0) {
            $line = $line.Substring(0, $hashIndex)
        }
        if ($line -match "^\s*$") {
            continue
        }
        if ($line -match "^resources:\s*$") {
            $inResources = $true
            continue
        }
        if ($inResources -and $line -match "^\S") {
            break
        }
        if ($inResources -and $line -match "^\s{2}-\s+(.+\.yaml)\s*$") {
            $resource = $Matches[1].Trim()
            if ($resource -match "[\\/]" -or $resource -eq "." -or $resource -eq ".." -or $resource -notmatch "^[A-Za-z0-9._-]+\.yaml$") {
                throw "Unsupported kustomization resource path '$resource'. Only root-level YAML files are allowed."
            }
            if ($resource -ne "01-secret.yaml" -and -not $resources.Contains($resource)) {
                $resources += $resource
            }
        }
    }

    if (-not $resources.Contains("01-secret.example.yaml")) {
        $resources += "01-secret.example.yaml"
    }

    $resources
}

function Copy-K8sRootYaml {
    param(
        [string]$From,
        [string]$To
    )
    if (-not (Test-Path -LiteralPath $From)) {
        throw "Source k8s directory not found: $From"
    }
    $kustomization = Join-Path $From "kustomization.yaml"
    $resources = Get-KustomizationResources -Path $kustomization

    if (Test-Path -LiteralPath $To) {
        Assert-PathInside -BasePath $OutputRoot -TargetPath $To -Purpose "render output"
        Assert-PathNotEqual -LeftPath $To -RightPath $RepoRoot -Message "Refusing to delete repository root: $To"
        Assert-PathNotEqual -LeftPath $To -RightPath $OutputRoot -Message "Refusing to delete render output root: $To"
        Assert-PathNotEqual -LeftPath $To -RightPath $From -Message "Refusing to delete source k8s directory: $To"
        Remove-Item -LiteralPath $To -Recurse -Force
    }
    New-Item -ItemType Directory -Path $To -Force | Out-Null

    Copy-Item -LiteralPath $kustomization -Destination (Join-Path $To "kustomization.yaml") -Force
    foreach ($resource in $resources) {
        $sourcePath = Join-Path $From $resource
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Kustomization resource file not found: $sourcePath"
        }
        Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $To $resource) -Force
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
    Assert-Contains -Path (Join-Path $Dir "09-api.yaml") -Needle "imagePullPolicy: IfNotPresent"

    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.Endpoint)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.StorageBucket)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.StoragePrefix)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "value: $($Minio.PublicUrl)"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "image: openrag/task-worker:$ImageTag"
    Assert-Contains -Path (Join-Path $Dir "10-task-worker.yaml") -Needle "imagePullPolicy: IfNotPresent"

    Assert-Contains -Path (Join-Path $Dir "11-web.yaml") -Needle "image: openrag/web:$ImageTag"
    Assert-Contains -Path (Join-Path $Dir "11-web.yaml") -Needle "imagePullPolicy: IfNotPresent"
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

Validate-ProfileName -Name $Profile
if (-not $ProfilePath) {
    $ProfilePath = Join-Path $RepoRoot "deploy\envs\$Profile.yaml"
}

$SourceDir = [System.IO.Path]::GetFullPath($SourceDir)
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$ProfilePath = [System.IO.Path]::GetFullPath($ProfilePath)
$OutputDir = [System.IO.Path]::GetFullPath((Join-Path $OutputRoot $Profile))

Validate-OutputRoot -Path $OutputRoot -SourcePath $SourceDir
Assert-PathInside -BasePath $OutputRoot -TargetPath $OutputDir -Purpose "render output directory"

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
        & $kubectl.Source kustomize $OutputDir | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "kubectl kustomize failed with exit code $LASTEXITCODE."
        }
    } else {
        Write-Host "kubectl not found; skipped kustomize validation."
    }
}

Write-Host "Rendered profile '$Profile' to $OutputDir with OpenRag image tag '$Tag'."
