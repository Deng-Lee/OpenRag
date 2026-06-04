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

function Assert-RendererFails {
    param(
        [string[]]$Arguments,
        [string]$ExpectedMessage
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = powershell -NoProfile -ExecutionPolicy Bypass -File $Renderer @Arguments 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    Assert-True -Condition ($exitCode -ne 0) -Message "Expected renderer to fail for arguments: $($Arguments -join ' ')"
    Assert-True -Condition $output.Contains($ExpectedMessage) -Message "Expected renderer failure to contain '$ExpectedMessage'. Actual output: $output"
}

function Remove-TestOutput {
    if (Test-Path -LiteralPath $OutputRoot) {
        $repoFull = [System.IO.Path]::GetFullPath($RepoRoot)
        $targetFull = [System.IO.Path]::GetFullPath($OutputRoot)
        $expectedFull = [System.IO.Path]::GetFullPath((Join-Path $repoFull ".tmp-rendered-test"))
        Assert-True -Condition $targetFull.Equals($expectedFull, [System.StringComparison]::OrdinalIgnoreCase) -Message "Refusing to remove unexpected path: $targetFull"
        Remove-Item -LiteralPath $OutputRoot -Recurse -Force
    }
}

try {
    Remove-TestOutput

    Assert-RendererFails -Arguments @("-Profile", "openrag", "-Tag", "9.9.9", "-OutputRoot", ".", "-SkipKustomize") -ExpectedMessage "OutputRoot must be named"
    Assert-RendererFails -Arguments @("-Profile", "09-api.yaml", "-Tag", "9.9.9", "-OutputRoot", "k8s", "-SkipKustomize") -ExpectedMessage "OutputRoot must be named"
    Assert-RendererFails -Arguments @("-Profile", "..\k8s", "-Tag", "9.9.9", "-OutputRoot", $OutputRoot, "-SkipKustomize") -ExpectedMessage "Invalid profile name"

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

    Assert-FileContains -Path (Join-Path $RenderedDir "13-configmap-openrag-llm.yaml") -Needle 'EMBEDDING_DIMENSION: "2560"'
    Assert-FileContains -Path (Join-Path $RenderedDir "09-api.yaml") -Needle "key: EMBEDDING_DIMENSION"
    Assert-FileContains -Path (Join-Path $RenderedDir "10-task-worker.yaml") -Needle "key: EMBEDDING_DIMENSION"

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
        & $kubectl.Source kustomize $RenderedDir | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "kubectl kustomize failed with exit code $LASTEXITCODE."
        }
    } else {
        Write-Host "kubectl not found; skipped kustomize smoke test."
    }

    Write-Host "render-k8s-profile tests passed"
}
finally {
    Remove-TestOutput
}
