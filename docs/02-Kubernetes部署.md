# OpenRag Kubernetes 部署指南

> 本文是 Kubernetes 部署的唯一维护入口。清单以 [`k8s/`](../k8s/) 和 [`k8s/README.md`](../k8s/README.md) 为准；镜像构建以 `docker/Dockerfile.*` 为准。示例均不得直接携带真实密码、Token 或 Access Key。

## 1. 适用范围与基本原则

- 适用于 OpenRag 全量部署、已有集群的业务增量发布，以及不能访问公网的内网集群。
- 仓库清单是单副本、开发/POC 基线；生产环境需自行补充高可用、备份、资源限额和 `StorageClass`。
- 内网部署必须在联网环境准备镜像、Python/npm 依赖和离线模型，进入内网后不再依赖公网。
- 已有集群的业务升级只更新业务镜像和相关 ConfigMap；除非本次明确变更基础设施，不要重新 apply PostgreSQL、Milvus、etcd、Elasticsearch 和 PVC 清单。
- Secret 只存敏感值，ConfigMap 只存非敏感配置；不要用示例或占位值覆盖集群中已有 Secret。

主要组件：

| 组件 | K8s 工作负载 | 关键依赖/检查 |
| --- | --- | --- |
| PostgreSQL | `StatefulSet/postgres` | PVC、备份、数据库迁移 |
| Milvus etcd | `Deployment/milvus-etcd` | PVC、`2379` 健康 |
| Milvus | `Deployment/milvus` | etcd、MinIO/S3、RocksMQ、`19530` |
| Elasticsearch | `StatefulSet/elasticsearch` | PVC、`vm.max_map_count >= 262144` |
| API | `Deployment/openrag-api`、`Service/api` | `GET /health`、容器端口 `8000` |
| Worker | `Deployment/openrag-task-worker` | API、MinIO、Milvus、模型与 OCR |
| Web | `Deployment/openrag-web`、`Service/web` | `/api` 反代、运行时 `app-config.js` |

## 2. 部署配置

### 2.1 本次 `1.1.9-qwen3.6` 内网配置

以下是不含密钥的已知配置。地址变更时以目标内网实际值为准。

| 配置 | 值/要求 |
| --- | --- |
| Namespace | `openrag` |
| 业务镜像版本 | `1.1.9-qwen3.6` |
| L1 导航模型 | `youfu/Qwen3.6-35B-A3B` |
| Milvus 镜像 | `milvusdb/milvus:v2.4.17` |
| 业务 MinIO | `http://172.16.31.63:9000` |
| 业务 bucket / prefix | `rag-kb` / `openrag` |
| Milvus bucket | `rag-kb`，必须使用独立 rootPath |
| LLM/Embedding 地址 | Pod 可访问的内网 OpenAI 兼容 `/v1` 地址 |
| PaddleOCR 地址 | Pod 可访问的内网 `PADDLEOCR_API_URL`，不能沿用外网地址 |

API 和 Worker 的对象存储配置必须一致：

```text
STORAGE_TYPE=minio
STORAGE_IMPL=MINIO
STORAGE_ENDPOINT=http://172.16.31.63:9000
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
STORAGE_ACCESS_KEY <- Secret/MINIO_ROOT_USER
STORAGE_SECRET_KEY <- Secret/MINIO_ROOT_PASSWORD
```

模型相关配置：

```text
OPENAI_BASE_URL=<内网 OpenAI 兼容地址，通常以 /v1 结尾>
OPENAI_API_KEY=<Secret>
L1_NAV_MODEL=youfu/Qwen3.6-35B-A3B
L1_NAV_BASE_URL=<可选；为空时回退 OPENAI_BASE_URL>
PADDLEOCR_API_URL=<内网 PaddleOCR 地址>
PADDLEOCR_ACCESS_TOKEN=<可选 Secret>
```

`PADDLEOCR_API_URL` 主要由 Worker 使用。部署前必须从 Pod 内验证地址，而不是只在宿主机验证。

当前 `09-api.yaml` / `10-task-worker.yaml` 已注入 `OPENAI_BASE_URL` 和 `L1_NAV_MODEL`，但没有直接注入 `PADDLEOCR_API_URL`，也没有注入独立的 `L1_NAV_BASE_URL`。内网需要这些配置时，必须同步扩展 ConfigMap/Secret 和对应 Deployment 的 `env`，仅修改一个未被 Pod 引用的 ConfigMap key 不会生效。

### 2.2 配置资源

| 文件 | 作用 | 注意 |
| --- | --- | --- |
| `01-secret.example.yaml` | Secret 模板 | 复制为不入库的实际 Secret；禁止保留 `CHANGE_ME_*` |
| `13-configmap-openrag-llm.yaml` | LLM、Embedding、L1 模型 | 内网地址和 `L1_NAV_MODEL` 必须按环境修改 |
| `14-configmap-openrag-web-runtime.yaml` | Web API 运行时地址 | 默认 `/api`，修改后重启 Web 即可 |
| `15-configmap-openrag-service-conf.yaml` | RAGFlow 兼容配置 | 不得在 ConfigMap 中保留真实 MinIO 密码 |
| `07-milvus-config.yaml` | Milvus 非敏感覆盖配置 | 凭证不得写入 ConfigMap，详见第 7 节 |

部署前扫描占位符和公网地址：

```bash
find k8s -name '*.yaml' ! -name '01-secret.example.yaml' -print0 |
  xargs -0 grep -nE 'CHANGE_ME|YOUR_|docker\.io|quay\.io|docker\.elastic\.co|milvusdb/milvus|minio/minio' || true
```

预期只出现已经明确允许的示例或待替换镜像；实际 Secret 中不得有 `CHANGE_ME_*`。

## 3. 联网构建机：生成离线制品

以下以 Windows PowerShell 为主。不要把 Bash 的 `export` 和续行符 `\` 复制到 PowerShell；PowerShell 环境变量使用 `$env:NAME`。使用数组传给 `docker save` 可以避免续行符错误。

在仓库根目录执行：

```powershell
$ErrorActionPreference = "Stop"

$repoRoot = "E:\project\OpenRag"
Set-Location $repoRoot

$env:TAG = "1.1.9-qwen3.6"
$env:BUILD_PROXY = "http://http.docker.internal:3128" # 不需要代理时设为 ""
$env:NPM_REGISTRY = "https://registry.npmjs.org"

docker info | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker daemon 不可用" }

foreach ($path in @("docker", "openrag", "web", "k8s")) {
  if (-not (Test-Path $path)) { throw "缺少仓库目录：$path" }
}

$requiredModelFiles = @(
  "det.onnx", "rec.onnx", "ocr.res", "layout.onnx",
  "tsr.onnx", "updown_concat_xgb.model"
)
foreach ($file in $requiredModelFiles) {
  $path = Join-Path "openrag/rag/res/deepdoc" $file
  if (-not (Test-Path $path)) { throw "Worker 离线模型缺失：$path" }
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
  docker pull $image
  if ($LASTEXITCODE -ne 0) { throw "镜像拉取失败：$image" }
}

docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.api -t "openrag/api:$($env:TAG)" .
if ($LASTEXITCODE -ne 0) { throw "API 镜像构建失败" }

docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.worker -t "openrag/task-worker:$($env:TAG)" .
if ($LASTEXITCODE -ne 0) { throw "Worker 镜像构建失败" }

docker build --build-arg NPM_PROXY=$env:BUILD_PROXY `
  --build-arg NPM_REGISTRY=$env:NPM_REGISTRY `
  -f docker/Dockerfile.web -t "openrag/web:$($env:TAG)" .
if ($LASTEXITCODE -ne 0) { throw "Web 镜像构建失败" }

$businessImages = @(
  "openrag/api:$($env:TAG)",
  "openrag/task-worker:$($env:TAG)",
  "openrag/web:$($env:TAG)"
)
$allImages = $thirdPartyImages + $businessImages

foreach ($image in $allImages) {
  docker image inspect $image | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "缺少镜像：$image" }
}

$tar = "openrag-offline-$($env:TAG)-images.tar"
docker save -o $tar $allImages
if ($LASTEXITCODE -ne 0) { throw "docker save 失败" }

$hash = (Get-FileHash $tar -Algorithm SHA256).Hash.ToLower()
"$hash  $tar" | Set-Content "$tar.sha256" -Encoding ascii
```

若本次只是已有集群的业务升级，可只打包 `$businessImages`，第三方镜像继续复用。推荐制品至少包含：

```text
openrag-k8s-business-<TAG>.tar
openrag-k8s-manifests-<TAG>.zip
release-manifest.json
SHA256SUMS
validation/
```

## 4. 搬运、导入和内网镜像仓库

内网收到制品后先校验：

```bash
sha256sum -c SHA256SUMS
docker load -i openrag-k8s-business-1.1.9-qwen3.6.tar
docker images | grep -E 'openrag/(api|task-worker|web).*1.1.9-qwen3.6'
```

### 4.1 优先使用 Harbor/内网 Registry

1. 将业务和缺失的第三方镜像重新打 tag 并推送到内网 Registry。
2. Kustomize overlay 必须覆盖 API、Worker、Web、busybox 以及本次需要同步的第三方镜像。
3. 渲染后确认 `image:` 不再指向公网仓库。
4. Registry 需要认证时，创建 `imagePullSecrets` 并关联 ServiceAccount。

```bash
kubectl -n openrag create secret docker-registry harbor-regcred \
  --docker-server='<内网仓库>' \
  --docker-username='<用户名>' \
  --docker-password='<Token>' \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n openrag patch serviceaccount default \
  -p '{"imagePullSecrets":[{"name":"harbor-regcred"}]}'

kubectl kustomize k8s/overlays/private-registry \
  --load-restrictor=LoadRestrictionsNone | grep 'image:'
```

### 4.2 无 Harbor 时按节点运行时导入

先确认 kubelet 的实际运行时：

```bash
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.addresses[?(@.type=="InternalIP")].address}{"\t"}{.status.nodeInfo.containerRuntimeVersion}{"\n"}{end}'
```

- `docker://...`：在每个可能调度业务 Pod 的节点执行 `docker load -i <tar>`。
- `containerd://...`：执行 `ctr -n k8s.io images import <tar>`。
- 不要把镜像只导入 containerd 后让 Docker runtime 的 kubelet使用；两个镜像存储彼此独立。
- 使用节点本地镜像时，清单设置 `imagePullPolicy: IfNotPresent`。

## 5. 部署方式

### 5.1 已有集群的业务增量发布

这是本次部署使用的模式。执行前备份当前对象：

```bash
export NS=openrag
export BACKUP_DIR="$PWD/backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

kubectl -n "$NS" get deploy,sts,cm -o yaml > "$BACKUP_DIR/workloads-configmaps.yaml"
kubectl -n "$NS" get secret openrag-secrets -o yaml > "$BACKUP_DIR/openrag-secrets.yaml"
chmod 600 "$BACKUP_DIR/openrag-secrets.yaml"
```

只更新以下业务资源：

```text
13-configmap-openrag-llm.yaml
14-configmap-openrag-web-runtime.yaml
15-configmap-openrag-service-conf.yaml
09-api.yaml
10-task-worker.yaml
11-web.yaml
12-ingress.yaml（有变化时）
```

不要在业务升级中 apply：

```text
03-postgres.yaml
05-milvus-etcd.yaml
06-milvus-minio.yaml
07-milvus-config.yaml
07-milvus.yaml
08-elasticsearch.yaml
```

应用前先确认业务清单中的镜像 tag 已经是本次版本；否则 `kubectl apply` 会把镜像回退到清单里的旧 tag。

若包含数据库变更，先用新 API 镜像独立运行：

```bash
python -m alembic upgrade head
python -m alembic current
```

首次引入 R-05 registry 时，使用与 API 相同的镜像显式注册 legacy Collection。命令默认只做 dry-run：

```bash
python -m openrag.cli.index_generation bootstrap-legacy --operator <变更单或操作者>
python -m openrag.cli.index_generation bootstrap-legacy --operator <变更单或操作者> --execute
```

执行前必须把 `EMBEDDING_REVISION` 和 `EMBEDDING_MODEL_IDENTITY` 配成当前线上模型的不可变 revision/digest。该命令不重命名、复制、创建或删除 Collection，也不修改 Alias；重复执行幂等。不要把 `--execute` 放进 API startup 或每次部署的 initContainer。

随后更新业务资源并验证 rollout：

```bash
kubectl apply -f 13-configmap-openrag-llm.yaml \
  -f 14-configmap-openrag-web-runtime.yaml \
  -f 15-configmap-openrag-service-conf.yaml \
  -f 09-api.yaml \
  -f 10-task-worker.yaml \
  -f 11-web.yaml

kubectl -n "$NS" rollout status deployment/openrag-api --timeout=600s
kubectl -n "$NS" rollout status deployment/openrag-task-worker --timeout=600s
kubectl -n "$NS" rollout status deployment/openrag-web --timeout=600s
```

### 5.2 首次或全量部署

当前使用外部 MinIO `172.16.31.63:9000`，不要同时部署 `06-milvus-minio.yaml`。只有明确使用集群内 MinIO 时才应用该文件，并同步修改 Milvus 和业务存储地址。

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-secret.yaml
kubectl apply -f k8s/03-postgres.yaml
kubectl apply -f k8s/05-milvus-etcd.yaml
kubectl apply -f k8s/07-milvus-config.yaml
kubectl apply -f k8s/07-milvus.yaml
kubectl apply -f k8s/08-elasticsearch.yaml
kubectl apply -f k8s/13-configmap-openrag-llm.yaml
kubectl apply -f k8s/14-configmap-openrag-web-runtime.yaml
kubectl apply -f k8s/15-configmap-openrag-service-conf.yaml

kubectl -n openrag rollout status statefulset/postgres --timeout=600s
kubectl -n openrag rollout status deployment/milvus-etcd --timeout=600s
kubectl -n openrag rollout status deployment/milvus --timeout=600s
kubectl -n openrag rollout status statefulset/elasticsearch --timeout=600s

# 完成数据库迁移后再启动业务
kubectl apply -f k8s/09-api.yaml
kubectl apply -f k8s/10-task-worker.yaml
kubectl apply -f k8s/11-web.yaml
kubectl apply -f k8s/12-ingress.yaml
```

## 6. 验收

### 6.1 镜像、Pod 与 Endpoint

```bash
export NS=openrag

kubectl -n "$NS" get deploy openrag-api openrag-task-worker openrag-web \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[*].image}{"\n"}{end}'

kubectl -n "$NS" get pods -o wide
kubectl -n "$NS" get endpoints api web milvus postgres elasticsearch
```

### 6.2 API 与配置

健康接口必须使用 `GET`，不要用 `curl -I` 的 HEAD 请求判断：

```bash
kubectl -n "$NS" exec deployment/openrag-api -c api -- \
  curl -fsS http://127.0.0.1:8000/health

kubectl -n "$NS" exec deployment/openrag-api -c api -- \
  printenv L1_NAV_MODEL
```

预期模型：

```text
youfu/Qwen3.6-35B-A3B
```

从 API/Worker Pod 内分别验证内网 MinIO、LLM 和 PaddleOCR 地址。HTTP `404` 说明网络已经到达服务，但请求路径或 API 协议不正确，不等于网络不通。

### 6.3 Milvus 与查询链路

```bash
kubectl -n "$NS" get pods -l app=milvus -o wide
kubectl -n "$NS" get endpointslice \
  -l kubernetes.io/service-name=milvus -o wide

kubectl -n "$NS" exec -i deployment/openrag-api -c api -- \
  env PYTHONWARNINGS=ignore python - <<'PY'
import os
import socket
from pymilvus import utility
from openrag.vectorstore.milvus_store import MilvusStore

host = os.environ.get("MILVUS_HOST", "milvus")
port = int(os.environ.get("MILVUS_PORT", "19530"))
dimension = int(os.environ.get("EMBEDDING_DIMENSION", "2560"))

with socket.create_connection((host, port), timeout=5):
    print("TCP_OK", host, port)

MilvusStore(dimension=dimension)
print("MILVUS_STORE_OK")
print("COLLECTIONS", utility.list_collections())
PY
```

最终至少完成一次：登录 → 上传小文件 → 解析任务成功 → 生成向量 → 查询命中。新建 Milvus 状态空间后，旧向量不会自动出现，必须重新处理文档。

## 7. 本次 Milvus CrashLoop：现状与处理

### 7.1 已确认现状

本次 `1.1.9-qwen3.6` 业务镜像升级后，查询提示 `Vector database unavailable`。已确认：

- API `/health` 和 PostgreSQL 正常。
- `milvus` DNS 能解析，但 `19530` 为 `Connection refused`。
- Milvus Pod 为 `ready=false`、持续重启，Service Endpoint 为 `ready=false`。
- 旧日志出现过 `The Access Key Id you provided does not exist`，并观察到 Milvus 使用默认 `a-bucket/files`，说明目标配置曾未生效。
- `kubectl exec ... container not found ("milvus")` 在本次场景中是容器正处于 CrashLoop 退出间隙，不是容器名称错误。
- `rollout successfully rolled out` 只代表曾短暂 Ready；必须继续观察 restartCount，不能据此宣布恢复。
- API 中 PyMilvus `2.6.16` 不是服务端进程崩溃的原因，但与 Milvus `2.4.17` 的跨度需要单独治理。

截至本文整理时，新的 Milvus 恢复方案尚未完成最终稳定性验收，不能标记为已解决。

### 7.2 排查顺序

不要先看 Go goroutine 堆栈尾部；先抓上一轮崩溃的第一条错误：

```bash
export NS=openrag
POD=$(kubectl -n "$NS" get pods -l app=milvus \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1].metadata.name}')
CONTAINER=$(kubectl -n "$NS" get pod "$POD" \
  -o jsonpath='{.spec.containers[0].name}')

kubectl -n "$NS" logs "$POD" -c "$CONTAINER" \
  --previous --timestamps > milvus-crash-previous.log 2>&1

grep -nEi -m 30 \
  'panic|fatal|access key|accessdenied|invalidaccesskey|failed to check blob|failed to connect|segmentation|signal' \
  milvus-crash-previous.log
```

同时确认退出原因、Endpoint 和依赖：

```bash
kubectl -n "$NS" get pod "$POD" \
  -o jsonpath='ready={.status.containerStatuses[0].ready}{"\n"}restarts={.status.containerStatuses[0].restartCount}{"\n"}reason={.status.containerStatuses[0].lastState.terminated.reason}{"\n"}exitCode={.status.containerStatuses[0].lastState.terminated.exitCode}{"\n"}'

kubectl -n "$NS" get endpointslice \
  -l kubernetes.io/service-name=milvus -o wide
```

### 7.3 配置与状态隔离要求

Milvus Standalone 的状态不只有 etcd 和 MinIO，还包括 PVC 上的 RocksMQ 和本地缓存。创建全新逻辑实例时必须同时隔离：

| 状态域 | 本次建议的新值 |
| --- | --- |
| `etcd.rootPath` | `openrag-119-qwen36-r2` |
| `minio.rootPath` | `milvus-119-qwen36-r2` |
| `rocksmq.path` | `/var/lib/milvus/rdb_data-119-qwen36-r2` |
| `localStorage.path` | `/var/lib/milvus/data-119-qwen36-r2` |
| `msgChannel.chanNamePrefix.cluster` | `openrag-119-qwen36-r2` |

建议将非敏感覆盖统一挂载到 `/milvus/configs/user.yaml`：

```yaml
etcd:
  endpoints:
    - milvus-etcd:2379
  rootPath: openrag-119-qwen36-r2

minio:
  address: 172.16.31.63
  port: 9000
  bucketName: rag-kb
  rootPath: milvus-119-qwen36-r2
  useSSL: false

mq:
  type: rocksmq
rocksmq:
  path: /var/lib/milvus/rdb_data-119-qwen36-r2
localStorage:
  path: /var/lib/milvus/data-119-qwen36-r2
msgChannel:
  chanNamePrefix:
    cluster: openrag-119-qwen36-r2
```

凭证只通过环境变量引用已有 Secret：

```text
MINIO_ACCESS_KEY_ID     <- openrag-secrets/MINIO_ROOT_USER
MINIO_SECRET_ACCESS_KEY <- openrag-secrets/MINIO_ROOT_PASSWORD
```

同时移除旧的 `/milvus/configs/milvus.yaml` ConfigMap 覆盖、`MILVUSCONF`、`/milvus/runtime/user.yaml` 和重复的 `MINIO_ADDRESS` 配置，避免多个来源互相覆盖。旧 PVC、旧 etcd root 和旧 MinIO prefix 不删除，以便回溯；新实例恢复后需要重建向量。

### 7.4 恢复验收门槛

Milvus Ready 后至少连续观察 180 秒：

```bash
POD=$(kubectl -n openrag get pods -l app=milvus \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1].metadata.name}')

BASE=$(kubectl -n openrag get pod "$POD" \
  -o jsonpath='{.status.containerStatuses[0].restartCount}')

for i in $(seq 1 18); do
  sleep 10
  READY=$(kubectl -n openrag get pod "$POD" \
    -o jsonpath='{.status.containerStatuses[0].ready}')
  RESTARTS=$(kubectl -n openrag get pod "$POD" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}')
  echo "check=$i ready=$READY restarts=$RESTARTS"
  test "$READY" = true
  test "$RESTARTS" = "$BASE"
done
```

只有同时满足以下条件才算恢复：

- `ready=true` 且 restartCount 180 秒内不增加；
- EndpointSlice 为 `ready=true`；
- API Pod 到 `milvus:19530` TCP 成功；
- `MilvusStore` 初始化成功；
- 小文件重新处理后可以查询。

## 8. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `failed to connect to docker API at npipe...` | Docker Desktop/daemon 未启动；先执行 `docker info` |
| PowerShell 不识别 `export` 或把镜像名当命令 | 混用了 Bash 语法；使用 `$env:TAG=...`，PowerShell 续行符为反引号且后面不能有空格 |
| `docker save requires at least 1 argument` | 多行命令被 PowerShell 拆断；改用镜像数组传给 `docker save` |
| `No such image` | 镜像未构建成功或 tag 不一致；保存前逐个 `docker image inspect` |
| Web 构建 `tsc not found` | 未安装 dev/optional dependencies；镜像构建需执行 `npm ci --include=dev --include=optional` |
| apt/npm 超时 | 构建容器无法出网；检查 `BUILD_PROXY`、`NPM_PROXY` 和内部镜像源 |
| Worker 离线运行时下载模型 | 离线模型未打入镜像；检查 `openrag/rag/res/deepdoc` 和禁用外网下载配置 |
| `ImagePullBackOff` | 镜像地址、busybox、拉取密钥或节点 runtime 导入方式错误；先看 `kubectl describe pod` Events |
| API init 一直等待 Milvus | API 通常不是根因；检查 Milvus Pod、Endpoint、etcd、MinIO 和 `--previous` 日志 |
| Milvus 日志显示 `a-bucket/files` | 自定义配置未加载，仍在使用默认配置；核对 `/milvus/configs/user.yaml` 和重复配置源 |
| MinIO `Access Key Id ... does not exist` | Milvus 使用了错误 AK/SK；用已验证的 `MINIO_ROOT_USER/PASSWORD`，不要打印密钥 |
| `container not found ("milvus")` 且 Pod restartCount 增长 | 容器正在 CrashLoop 退出间隙；使用 `kubectl logs --previous`，不要继续 `exec` |
| `rollout successfully rolled out` 后又失败 | Pod 曾短暂 Ready；增加稳定性观察和 restartCount 门槛 |
| PaddleOCR 返回 404 | 网络通常已通，但 base URL/路径不符合服务协议；确认内部接口完整路径和请求格式 |
| `curl -I /health` 返回 405 | `-I` 是 HEAD；健康检查使用 `GET` |
| `Vector database unavailable` | API 无法初始化 Milvus；先确认 Endpoint 和 TCP，再看服务端 CrashLoop |
| Milvus 已稳定但 PyMilvus 调用仍异常 | 核对客户端/服务端兼容性；当前宽泛依赖可能安装 PyMilvus `2.6.x`，生产制品建议与 Milvus `2.4.x` 锁定并在外网重建 API/Worker |

## 9. 回滚与安全

- 禁止未经确认执行 `git reset --hard`、删除 PVC、清空 etcd 或删除 MinIO prefix。
- `kubectl rollout undo` 只回滚 Deployment PodTemplate，不回滚 ConfigMap、Secret 和 PVC。
- 排障产生多个 revision 后，必须先查看历史，再指定稳定 revision：

```bash
kubectl -n openrag rollout history deployment/<name>
kubectl -n openrag rollout undo deployment/<name> --to-revision=<稳定版本>
```

- 本地 Secret 备份必须 `chmod 600`，用完按内规销毁。
- 如果恢复使用新的 Milvus 状态空间，旧向量数据保持不动但不会自动可见；重新索引前先确认业务原文件仍可访问。

## 10. 发布检查清单

- [ ] 版本 tag 在构建、离线包、清单和集群中一致
- [ ] 第三方镜像和 `busybox:1.36` 已在内网可用
- [ ] Worker 离线模型文件完整
- [ ] 实际清单无 `CHANGE_ME_*`、`YOUR_*` 和未批准的公网地址
- [ ] MinIO endpoint、bucket、业务 prefix 与 Milvus rootPath 已区分
- [ ] LLM、Embedding、PaddleOCR 地址能从 Pod 内访问
- [ ] `L1_NAV_MODEL=youfu/Qwen3.6-35B-A3B`
- [ ] 数据库迁移已完成并验证 revision
- [ ] API、Worker、Web 镜像为目标 tag
- [ ] PostgreSQL、Milvus、Elasticsearch 与业务 Endpoint 均 Ready
- [ ] Milvus restartCount 在稳定性窗口内不增加
- [ ] 健康检查、上传、解析、向量写入和查询闭环通过

## 11. 参考文件

- [`k8s/README.md`](../k8s/README.md)：清单入口与文件顺序
- [`k8s/kustomization.yaml`](../k8s/kustomization.yaml)：基线资源
- [`docker/docker-compose.prod.yml`](../docker/docker-compose.prod.yml)：依赖拓扑和环境变量参考
- `docker/Dockerfile.api`、`docker/Dockerfile.worker`、`docker/Dockerfile.web`：业务镜像构建入口
- [`01-项目说明.md`](./01-项目说明.md)、[`03-使用说明.md`](./03-使用说明.md)、[`04-外部系统接入与API.md`](./04-外部系统接入与API.md)
