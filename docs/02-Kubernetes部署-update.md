# OpenRag Kubernetes 部署说明（更新版）

> 本文基于 2026-05-18 在 Windows PowerShell + Docker Desktop 环境中实际构建、打包时遇到的问题整理。
> 目标是提供一条可重复执行的离线 K8s 部署路径。下一次优先按“下一次直接执行的 PowerShell 指令指南”操作；后面的章节用于解释原因、推送 Harbor 与部署 K8s。

## 下一次直接执行的 PowerShell 指令指南

这一节是主流程。下一次在 Windows PowerShell 中构建镜像和生成离线包时，优先执行这里的命令。

执行前只需要按你的实际情况修改这几项：

- `$repoRoot`：仓库根目录。
- `$env:TAG`：本次镜像版本，必须全程一致。
- `$env:BUILD_PROXY`：构建容器访问外网用的代理；Docker Desktop 常见值是 `http://http.docker.internal:3128`。如果你的网络不需要代理，可以设为空字符串 `""`。
- `$env:NPM_REGISTRY`：npm registry，默认使用官方 registry；如公司有内网 npm 镜像，改成内网地址。

### A. 构建业务镜像并生成离线包

下面整段可以直接在 PowerShell 中执行。它使用 PowerShell 数组传参，不依赖行尾反引号，所以不会再触发 `docker save requires at least 1 argument` 这类续行符问题。

```powershell
$ErrorActionPreference = "Stop"

$repoRoot = "E:\project\OpenRag"
Set-Location $repoRoot

$env:TAG = "1.0.9"
$env:BUILD_PROXY = "http://http.docker.internal:3128"
$env:NPM_REGISTRY = "https://registry.npmjs.org"
$env:OFFLINE_TAR = "openrag-offline-$($env:TAG)-images.tar"

function Invoke-Checked {
  param(
    [Parameter(Mandatory = $true)][string]$Step,
    [Parameter(Mandatory = $true)][scriptblock]$Command
  )

  Write-Host ""
  Write-Host "==> $Step"
  & $Command
  if ($LASTEXITCODE -ne 0) {
    throw "$Step 失败，退出码：$LASTEXITCODE"
  }
}

foreach ($path in @("docker", "openrag", "web", "k8s")) {
  if (-not (Test-Path $path)) {
    throw "当前目录不是 OpenRag 仓库根目录，缺少：$path"
  }
}

$requiredModelFiles = @(
  "det.onnx",
  "rec.onnx",
  "ocr.res",
  "layout.onnx",
  "tsr.onnx",
  "updown_concat_xgb.model"
)

foreach ($file in $requiredModelFiles) {
  $modelPath = Join-Path "openrag/rag/res/deepdoc" $file
  if (-not (Test-Path $modelPath)) {
    throw "Worker 离线模型文件缺失：$modelPath"
  }
}

Invoke-Checked "检查 Docker Desktop / Docker daemon" {
  docker info | Out-Null
}

$thirdPartyImages = @(
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "minio/minio:RELEASE.2023-03-20T20-16-18Z",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2",
  "busybox:1.36"
)

foreach ($image in $thirdPartyImages) {
  Invoke-Checked "拉取第三方镜像 $image" {
    docker pull $image
  }
}

Invoke-Checked "构建 API 镜像 openrag/api:$($env:TAG)" {
  docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY -f docker/Dockerfile.api -t "openrag/api:$($env:TAG)" .
}

Invoke-Checked "构建 Worker 镜像 openrag/task-worker:$($env:TAG)" {
  docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY -f docker/Dockerfile.worker -t "openrag/task-worker:$($env:TAG)" .
}

Invoke-Checked "构建 Web 镜像 openrag/web:$($env:TAG)" {
  docker build --build-arg NPM_PROXY=$env:BUILD_PROXY --build-arg NPM_REGISTRY=$env:NPM_REGISTRY -f docker/Dockerfile.web -t "openrag/web:$($env:TAG)" .
}

$requiredImages = @(
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "minio/minio:RELEASE.2023-03-20T20-16-18Z",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2",
  "busybox:1.36",
  "openrag/api:$($env:TAG)",
  "openrag/task-worker:$($env:TAG)",
  "openrag/web:$($env:TAG)"
)

foreach ($image in $requiredImages) {
  Invoke-Checked "校验本地镜像存在 $image" {
    docker image inspect $image | Out-Null
  }
}

Invoke-Checked "生成离线镜像包 $($env:OFFLINE_TAR)" {
  docker save -o $env:OFFLINE_TAR $requiredImages
}

$hash = (Get-FileHash $env:OFFLINE_TAR -Algorithm SHA256).Hash.ToLower()
"$hash  $env:OFFLINE_TAR" | Set-Content "$($env:OFFLINE_TAR).sha256" -Encoding ascii

Write-Host ""
Write-Host "离线包已生成：$($env:OFFLINE_TAR)"
Write-Host "校验文件已生成：$($env:OFFLINE_TAR).sha256"
docker images | Select-String "postgres|etcd|minio|milvus|elasticsearch|busybox|openrag"
```

### B. 构建成功后推送到内网 Harbor

在已经 `docker load -i openrag-offline-1.0.9-images.tar` 的内网制品机上执行。先修改 Harbor 地址、项目名和 TAG。

```powershell
$ErrorActionPreference = "Stop"

$env:TAG = "1.0.9"
$env:HARBOR = "harbor.internal.example"
$env:HARBOR_PROJECT = "openrag"

docker login $env:HARBOR
if ($LASTEXITCODE -ne 0) {
  throw "docker login 失败：$($env:HARBOR)"
}

$imageMap = @(
  [pscustomobject]@{ Source = "postgres:16-alpine"; Target = "postgres:16-alpine" },
  [pscustomobject]@{ Source = "quay.io/coreos/etcd:v3.5.5"; Target = "etcd:v3.5.5" },
  [pscustomobject]@{ Source = "minio/minio:RELEASE.2023-03-20T20-16-18Z"; Target = "minio:RELEASE.2023-03-20T20-16-18Z" },
  [pscustomobject]@{ Source = "milvusdb/milvus:v2.4.17"; Target = "milvus:v2.4.17" },
  [pscustomobject]@{ Source = "docker.elastic.co/elasticsearch/elasticsearch:8.12.2"; Target = "elasticsearch:8.12.2" },
  [pscustomobject]@{ Source = "busybox:1.36"; Target = "busybox:1.36" },
  [pscustomobject]@{ Source = "openrag/api:$($env:TAG)"; Target = "api:$($env:TAG)" },
  [pscustomobject]@{ Source = "openrag/task-worker:$($env:TAG)"; Target = "task-worker:$($env:TAG)" },
  [pscustomobject]@{ Source = "openrag/web:$($env:TAG)"; Target = "web:$($env:TAG)" }
)

foreach ($item in $imageMap) {
  $target = "$($env:HARBOR)/$($env:HARBOR_PROJECT)/$($item.Target)"

  docker tag $item.Source $target
  if ($LASTEXITCODE -ne 0) {
    throw "docker tag 失败：$($item.Source) -> $target"
  }

  docker push $target
  if ($LASTEXITCODE -ne 0) {
    throw "docker push 失败：$target"
  }
}
```

### C. 内网 K8s 部署入口

推送 Harbor 后，继续执行本文后面的“内网 K8s：生成私有仓库 Kustomize 覆盖”和“内网 K8s：创建 Secret 并部署”两节。部署前一定先渲染检查镜像：

```powershell
kubectl kustomize k8s/overlays/private-registry --load-restrictor=LoadRestrictionsNone |
  Select-String "image:"
```

输出中不应再出现 `quay.io`、`docker.elastic.co`、`minio/minio`、`milvusdb/milvus`、`openrag/api` 这类公网或本地镜像名。

## 问题记录：今天遇到的问题与处理结论

| 报错现象 | 根因 | 更新版处理 |
| --- | --- | --- |
| `failed to connect to the docker API at npipe... dockerDesktopLinuxEngine` | Docker Desktop / Docker daemon 未启动，或当前 Docker context 不可用 | 先执行 `docker info`，确认 Docker Desktop 已启动 |
| `export : 无法将“export”项识别为 cmdlet` | `export TAG=...` 是 Bash 语法，不是 PowerShell 语法 | PowerShell 使用 `$env:TAG="1.0.9"` |
| `docker save requires at least 1 argument`，随后每个镜像名都被 PowerShell 当成命令 | 把 Bash 的续行符 `\` 直接复制到了 PowerShell | PowerShell 多行命令必须使用反引号 `` ` ``，并且反引号后不能有空格 |
| `No such image: openrag/api:1.0.9` | 执行 `docker save` 前业务镜像没有构建成功，或 TAG 不一致 | `docker save` 前先用 `docker image inspect` 校验所有镜像 |
| Web 构建时报 `sh: tsc: not found` | 前端构建脚本需要 `typescript` / `vite`，它们在 `devDependencies` 中；依赖未完整安装时就会找不到 `tsc` | `docker/Dockerfile.web` 使用 `npm ci --include=dev --include=optional`，并校验 `typescript` / `vite` |
| npm 报 `ETIMEDOUT`、`ELSPROBLEMS`、`Exit handler never called` | 容器内 npm 访问 registry 网络不稳定，依赖安装不完整 | 构建时显式传入 `NPM_PROXY`，默认 registry 使用 `https://registry.npmjs.org` |
| API / Worker 构建时报 `Unable to connect to deb.debian.org`、`Unable to locate package gcc/curl` | 容器内 apt 访问 Debian 源超时，索引未下载完整 | Dockerfile 自动写入 Docker Desktop 代理；必要时显式传入 `BUILD_PROXY` |
| Worker 构建下载 LibreOffice 依赖很慢或失败 | 完整 GUI 版 LibreOffice 依赖包过多，内网/代理环境容易超时 | Worker 使用 `libreoffice-writer-nogui`、`libreoffice-impress-nogui`、`--no-install-recommends` |
| Worker 构建校验时外连 `cl100k_base.tiktoken` 超时 | `tiktoken.get_encoding()` 在构建导入阶段触发外部下载 | `token_utils.py` 已改为懒加载，并提供离线 fallback |
| Worker 导入时报 `libGL.so.1` 缺失 | RAGFlow / OCR 链路需要 OpenCV 运行库 | Worker 镜像补充安装 `libgl1` |
| 内网 K8s `ImagePullBackOff` | 离线包漏了 K8s initContainer 用到的 `busybox:1.36`，或 YAML 仍指向公网镜像 | 本文把 `busybox:1.36` 加入拉取、保存、推送、Kustomize 覆盖流程 |

## 1. 适用前提

- 在仓库根目录执行命令，例如：`E:\project\OpenRag`。
- 本机已启动 Docker Desktop，并且 `docker info` 可以正常输出。
- 当前文档以 Windows PowerShell 为主；Linux / macOS 只使用 Bash 示例。
- 本文示例 TAG 使用 `1.0.9`，如需换版本，必须全程保持一致。
- Worker 构建依赖 `openrag/rag/res/deepdoc/` 下的离线模型文件，至少包括：
  - `det.onnx`
  - `rec.onnx`
  - `ocr.res`
  - `layout.onnx`
  - `tsr.onnx`
  - `updown_concat_xgb.model`

## 2. PowerShell 基础约定

PowerShell 设置环境变量：

```powershell
$env:TAG="1.0.9"
```

不要在 PowerShell 中执行：

```bash
export TAG=1.0.9
```

PowerShell 多行命令使用反引号 `` ` ``，并且反引号必须是该行最后一个字符：

```powershell
docker save -o $env:OFFLINE_TAR `
  postgres:16-alpine `
  busybox:1.36
```

不要把 Dockerfile 里的 `RUN ...` 直接复制到 PowerShell 里执行。`RUN` 是 Dockerfile 指令；终端里只执行 `docker build ...`。

## 3. 构建机预检查

在仓库根目录执行：

```powershell
docker info
```

如果出现 `failed to connect to the docker API at npipe...`，先启动 Docker Desktop，等左下角 Docker 状态正常后再继续。

确认当前目录是仓库根目录：

```powershell
Get-ChildItem docker, openrag, web, k8s
```

确认 Worker 离线模型文件存在：

```powershell
Get-ChildItem openrag/rag/res/deepdoc |
  Select-String "det.onnx|rec.onnx|ocr.res|layout.onnx|tsr.onnx|updown_concat_xgb.model"
```

如果构建环境通过 Docker Desktop 代理出网，设置：

```powershell
$env:BUILD_PROXY="http://http.docker.internal:3128"
```

如果你的环境不是 Docker Desktop 代理，把上面的值替换成真实代理，例如：

```powershell
$env:BUILD_PROXY="http://proxy.example.com:8080"
```

## 4. 联网构建机：拉取第三方镜像

K8s 清单中除了 Compose 里的中间件镜像，还使用了 `busybox:1.36` 作为 initContainer，所以离线包必须包含它。

```powershell
$thirdPartyImages = @(
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "minio/minio:RELEASE.2023-03-20T20-16-18Z",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2",
  "busybox:1.36"
)

foreach ($image in $thirdPartyImages) {
  docker pull $image
  if ($LASTEXITCODE -ne 0) {
    throw "镜像拉取失败：$image"
  }
}
```

校验：

```powershell
docker images | Select-String "postgres|etcd|minio|milvus|elasticsearch|busybox"
```

## 5. 联网构建机：构建业务镜像

设置统一 TAG：

```powershell
$env:TAG="1.0.9"
$env:BUILD_PROXY="http://http.docker.internal:3128"
```

构建 API：

```powershell
docker build `
  --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.api `
  -t "openrag/api:$($env:TAG)" `
  .
```

构建 Task Worker：

```powershell
docker build `
  --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.worker `
  -t "openrag/task-worker:$($env:TAG)" `
  .
```

构建 Web：

```powershell
docker build `
  --build-arg NPM_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.web `
  -t "openrag/web:$($env:TAG)" `
  .
```

如果你的 npm 只能访问内部 npm 镜像，可以额外传入：

```powershell
docker build `
  --build-arg NPM_PROXY=$env:BUILD_PROXY `
  --build-arg NPM_REGISTRY=https://your-npm-registry.example.com `
  -f docker/Dockerfile.web `
  -t "openrag/web:$($env:TAG)" `
  .
```

构建成功的关键日志包括：

- API：`openrag.api.main import OK`
- Worker：`offline ragflow model bundle OK`、`task_worker OK`
- Web：`npm run build` 完成，并出现 `naming to docker.io/openrag/web:<TAG>`

## 6. 联网构建机：保存离线包前校验镜像

这一步用于避免 `docker save` 时出现 `No such image`。

```powershell
$env:TAG="1.0.9"

$requiredImages = @(
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "minio/minio:RELEASE.2023-03-20T20-16-18Z",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2",
  "busybox:1.36",
  "openrag/api:$($env:TAG)",
  "openrag/task-worker:$($env:TAG)",
  "openrag/web:$($env:TAG)"
)

foreach ($image in $requiredImages) {
  docker image inspect $image | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "缺少镜像，不能继续 docker save：$image"
  }
}
```

查看镜像：

```powershell
docker images | Select-String "postgres|etcd|minio|milvus|elasticsearch|busybox|openrag"
```

## 7. 联网构建机：打包离线镜像

PowerShell 可直接执行下面整段。注意每个反引号后面不能有空格。

```powershell
$env:TAG="1.0.9"
$env:OFFLINE_TAR="openrag-offline-$($env:TAG)-images.tar"

docker save -o $env:OFFLINE_TAR `
  postgres:16-alpine `
  quay.io/coreos/etcd:v3.5.5 `
  minio/minio:RELEASE.2023-03-20T20-16-18Z `
  milvusdb/milvus:v2.4.17 `
  docker.elastic.co/elasticsearch/elasticsearch:8.12.2 `
  busybox:1.36 `
  openrag/api:$env:TAG `
  openrag/task-worker:$env:TAG `
  openrag/web:$env:TAG
```

生成 SHA256 校验文件：

```powershell
$hash = (Get-FileHash $env:OFFLINE_TAR -Algorithm SHA256).Hash.ToLower()
"$hash  $env:OFFLINE_TAR" | Set-Content "$($env:OFFLINE_TAR).sha256" -Encoding ascii
Get-Content "$($env:OFFLINE_TAR).sha256"
```

得到的文件：

- `openrag-offline-1.0.9-images.tar`
- `openrag-offline-1.0.9-images.tar.sha256`

## 8. 搬运到内网机器并导入

把上一步生成的两个文件复制到内网制品机或可访问内网 Harbor 的机器。

PowerShell 校验：

```powershell
$env:TAG="1.0.9"
$env:OFFLINE_TAR="openrag-offline-$($env:TAG)-images.tar"

$expected = (Get-Content "$($env:OFFLINE_TAR).sha256").Split(" ")[0]
$actual = (Get-FileHash $env:OFFLINE_TAR -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) {
  throw "离线包 SHA256 校验失败"
}
```

导入镜像：

```powershell
docker load -i $env:OFFLINE_TAR
```

确认镜像已导入：

```powershell
docker images | Select-String "postgres|etcd|minio|milvus|elasticsearch|busybox|openrag"
```

## 9. 内网制品机：推送到 Harbor

设置 Harbor 参数：

```powershell
$env:TAG="1.0.9"
$env:HARBOR="harbor.internal.example"
$env:HARBOR_PROJECT="openrag"
```

登录 Harbor：

```powershell
docker login $env:HARBOR
```

统一 retag 并 push：

```powershell
$imageMap = @(
  [pscustomobject]@{ Source="postgres:16-alpine"; Target="postgres:16-alpine" },
  [pscustomobject]@{ Source="quay.io/coreos/etcd:v3.5.5"; Target="etcd:v3.5.5" },
  [pscustomobject]@{ Source="minio/minio:RELEASE.2023-03-20T20-16-18Z"; Target="minio:RELEASE.2023-03-20T20-16-18Z" },
  [pscustomobject]@{ Source="milvusdb/milvus:v2.4.17"; Target="milvus:v2.4.17" },
  [pscustomobject]@{ Source="docker.elastic.co/elasticsearch/elasticsearch:8.12.2"; Target="elasticsearch:8.12.2" },
  [pscustomobject]@{ Source="busybox:1.36"; Target="busybox:1.36" },
  [pscustomobject]@{ Source="openrag/api:$($env:TAG)"; Target="api:$($env:TAG)" },
  [pscustomobject]@{ Source="openrag/task-worker:$($env:TAG)"; Target="task-worker:$($env:TAG)" },
  [pscustomobject]@{ Source="openrag/web:$($env:TAG)"; Target="web:$($env:TAG)" }
)

foreach ($item in $imageMap) {
  $target = "$($env:HARBOR)/$($env:HARBOR_PROJECT)/$($item.Target)"
  docker tag $item.Source $target
  if ($LASTEXITCODE -ne 0) {
    throw "docker tag 失败：$($item.Source) -> $target"
  }

  docker push $target
  if ($LASTEXITCODE -ne 0) {
    throw "docker push 失败：$target"
  }
}
```

推送后，K8s 清单中的所有镜像都应该改成内网 Harbor 地址，不能残留公网镜像。

## 10. 内网 K8s：生成私有仓库 Kustomize 覆盖

当前仓库已有 `k8s/overlays/private-registry/kustomization.yaml`，但原始示例只覆盖业务镜像。离线集群需要覆盖所有镜像，包括第三方镜像和 `busybox`。

在仓库根目录执行：

```powershell
$env:TAG="1.0.9"
$env:HARBOR="harbor.internal.example"
$env:HARBOR_PROJECT="openrag"

@"
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: openrag

resources:
  - ../../00-namespace.yaml
  - ../../02-configmap-nginx.yaml
  - ../../03-postgres.yaml
  - ../../05-milvus-etcd.yaml
  - ../../06-milvus-minio.yaml
  - ../../07-milvus.yaml
  - ../../08-elasticsearch.yaml
  - ../../13-configmap-openrag-llm.yaml
  - ../../14-configmap-openrag-web-runtime.yaml
  - ../../09-api.yaml
  - ../../10-task-worker.yaml
  - ../../11-web.yaml
  - ../../12-ingress.yaml

images:
  - name: postgres
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/postgres
    newTag: "16-alpine"
  - name: quay.io/coreos/etcd
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/etcd
    newTag: "v3.5.5"
  - name: minio/minio
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/minio
    newTag: "RELEASE.2023-03-20T20-16-18Z"
  - name: milvusdb/milvus
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/milvus
    newTag: "v2.4.17"
  - name: docker.elastic.co/elasticsearch/elasticsearch
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/elasticsearch
    newTag: "8.12.2"
  - name: busybox
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/busybox
    newTag: "1.36"
  - name: openrag/api
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/api
    newTag: "$($env:TAG)"
  - name: openrag/task-worker
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/task-worker
    newTag: "$($env:TAG)"
  - name: openrag/web
    newName: $($env:HARBOR)/$($env:HARBOR_PROJECT)/web
    newTag: "$($env:TAG)"
"@ | Set-Content -Path k8s/overlays/private-registry/kustomization.yaml -Encoding utf8
```

渲染检查，确认没有公网镜像残留：

```powershell
kubectl kustomize k8s/overlays/private-registry --load-restrictor=LoadRestrictionsNone |
  Select-String "image:"
```

如果输出里仍有 `docker.io`、`quay.io`、`docker.elastic.co`、`openrag/api` 这类公网或本地镜像名，需要先修正 overlay 再部署。

## 11. 内网 K8s：创建 Secret 并部署

从示例复制 Secret。如果 `k8s/01-secret.yaml` 已存在，不要覆盖，直接编辑现有文件：

```powershell
if (-not (Test-Path k8s/01-secret.yaml)) {
  Copy-Item k8s/01-secret.example.yaml k8s/01-secret.yaml
}
```

编辑 `k8s/01-secret.yaml`，至少确认这些值符合内网环境：

- `POSTGRES_PASSWORD`
- `SECRET_KEY`
- `MINIO_ROOT_USER`
- `MINIO_ROOT_PASSWORD`
- `OPENAI_API_KEY`
- 内网可访问的 LLM / Embedding 配置

先创建命名空间和业务 Secret：

```powershell
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-secret.yaml
```

如果 Harbor 需要认证，创建拉取密钥：

```powershell
kubectl -n openrag create secret docker-registry harbor-regcred `
  --docker-server=$env:HARBOR `
  --docker-username="YOUR_USERNAME" `
  --docker-password="YOUR_PASSWORD_OR_TOKEN" `
  --dry-run=client -o yaml |
  kubectl apply -f -

kubectl -n openrag patch serviceaccount default -p '{"imagePullSecrets":[{"name":"harbor-regcred"}]}'
```

部署：

```powershell
kubectl kustomize k8s/overlays/private-registry --load-restrictor=LoadRestrictionsNone |
  kubectl apply -f -
```

等待基础组件：

```powershell
kubectl -n openrag rollout status statefulset/postgres --timeout=600s
kubectl -n openrag rollout status deployment/milvus-etcd --timeout=600s
kubectl -n openrag rollout status deployment/milvus-minio --timeout=600s
kubectl -n openrag rollout status deployment/milvus --timeout=600s
kubectl -n openrag rollout status statefulset/elasticsearch --timeout=600s
```

等待业务组件：

```powershell
kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
kubectl -n openrag rollout status deployment/openrag-task-worker --timeout=600s
kubectl -n openrag rollout status deployment/openrag-web --timeout=600s
```

查看状态：

```powershell
kubectl -n openrag get pods,svc,ingress
```

## 12. 验证访问

没有 Ingress 时，可以先用 port-forward 验证 Web：

```powershell
kubectl -n openrag port-forward svc/web 8080:80
```

浏览器访问：

```text
http://127.0.0.1:8080
```

验证 API：

```powershell
kubectl -n openrag port-forward svc/api 8000:8000
```

另开一个 PowerShell：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health
```

## 13. 常见错误快速定位

### Docker daemon 连接失败

现象：

```text
failed to connect to the docker API at npipe...
```

处理：

```powershell
docker info
```

如果仍失败，启动 Docker Desktop，等待 Docker Engine 状态正常。

### PowerShell 把镜像名当成命令

现象：

```text
postgres:16-alpine : 无法将“postgres:16-alpine”项识别为 cmdlet
```

原因是用了 Bash 的 `\`。PowerShell 要用反引号 `` ` ``，或者把命令写成一行。

### Web 构建 `tsc not found`

不要在本机手动改 `node_modules`。使用本文第 5 节的 `docker build` 命令，确保 Dockerfile 执行：

```text
npm ci --include=dev --include=optional
```

### apt 下载失败

现象：

```text
Unable to connect to deb.debian.org
Unable to connect to http.docker.internal:3128
Unable to locate package gcc
```

处理思路：

1. 确认 Docker Desktop 代理是否可用。
2. 使用第 5 节中的 `--build-arg BUILD_PROXY=$env:BUILD_PROXY`。
3. 如果公司代理不是 Docker Desktop 代理，把 `$env:BUILD_PROXY` 换成公司代理地址。

### docker save 报缺镜像

现象：

```text
Error response from daemon: No such image: openrag/api:1.0.9
```

处理：

1. 确认 `$env:TAG` 与构建时一致。
2. 重新执行第 6 节镜像校验。
3. 缺哪个镜像，就先回到第 4 或第 5 节补齐。

### K8s ImagePullBackOff

先看 Pod 事件：

```powershell
$podName = "替换为实际 Pod 名称"
kubectl -n openrag describe pod $podName
```

重点检查：

- 镜像地址是否仍是公网地址。
- `busybox:1.36` 是否已推送到 Harbor。
- Harbor 是否需要 `imagePullSecrets`。
- 节点是否能解析并访问 `$env:HARBOR`。

## 14. Linux / macOS Bash 对照

如果你在 Linux / macOS 上执行，环境变量和续行符写法不同：

```bash
export TAG=1.0.9
export OFFLINE_TAR=openrag-offline-${TAG}-images.tar

docker save -o "${OFFLINE_TAR}" \
  postgres:16-alpine \
  quay.io/coreos/etcd:v3.5.5 \
  minio/minio:RELEASE.2023-03-20T20-16-18Z \
  milvusdb/milvus:v2.4.17 \
  docker.elastic.co/elasticsearch/elasticsearch:8.12.2 \
  busybox:1.36 \
  openrag/api:${TAG} \
  openrag/task-worker:${TAG} \
  openrag/web:${TAG}
```

不要把这段 Bash 命令直接复制到 PowerShell。
