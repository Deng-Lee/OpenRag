# OpenRag 外部 MinIO 模式 Kubernetes 部署策略

本文档用于指导 OpenRag 在 **MinIO 已迁移到外部单 bucket** 后的打包、镜像分发、Kubernetes 部署与验收流程。

与原始 `k8s/README.md` 的差异：

- 原始清单包含集群内 `milvus-minio`，适合最小 POC 或未迁移 MinIO 的环境。
- 本策略以外部 MinIO 为生产对象存储，不再把 `k8s/06-milvus-minio.yaml` 作为正式部署依赖。
- API、Worker、Milvus 同时使用外部 MinIO，但使用不同前缀隔离数据。

## 1. 目标部署拓扑

目标拓扑：

```text
Browser
  ↓
Ingress / Web Nginx
  ↓
openrag-web
  ↓ /api
openrag-api
  ↓
Postgres: postgres:5432
Milvus: milvus:19530
Elasticsearch: elasticsearch:9200
外部 MinIO: 172.16.31.63:9000/rag-kb
  ├── openrag/       # OpenRag 业务文件
  ├── milvus/        # Milvus restore 后的在线向量对象
  └── milvus-backups/# Milvus backup 中转文件
```

固定对象存储约定：

| 用途 | endpoint | bucket | prefix/rootPath |
| --- | --- | --- | --- |
| OpenRag API / Worker 业务文件 | `http://172.16.31.63:9000` | `rag-kb` | `openrag` |
| Milvus 在线向量对象 | `172.16.31.63:9000` | `rag-kb` | `milvus` |
| Milvus Backup 中转数据 | `http://172.16.31.63:9000` | `rag-kb` | `milvus-backups/<timestamp>` |

## 2. 本地清单调整策略

正式部署前，重点检查这些文件：

```text
k8s/01-secret.yaml
k8s/07-milvus-config.yaml
k8s/07-milvus.yaml
k8s/09-api.yaml
k8s/10-task-worker.yaml
k8s/15-configmap-openrag-service-conf.yaml
k8s/kustomization.yaml
k8s/overlays/private-registry/kustomization.yaml
```

### 2.1 Secret

从示例复制真实部署文件：

```powershell
if (-not (Test-Path k8s/01-secret.yaml)) {
  Copy-Item k8s/01-secret.example.yaml k8s/01-secret.yaml
}
```

编辑 `k8s/01-secret.yaml`，至少替换：

```yaml
POSTGRES_PASSWORD: "<Postgres 密码>"
SECRET_KEY: "<JWT 随机密钥>"
MINIO_ROOT_USER: "<外部 MinIO access key>"
MINIO_ROOT_PASSWORD: "<外部 MinIO secret key>"
MILVUS_SECRET_KEY: "<外部 MinIO secret key 或 Milvus 所需 secret>"
OPENAI_API_KEY: "<模型网关或 OpenAI key>"
```

注意：

- `k8s/01-secret.yaml` 不应提交到 Git。
- `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 在本策略中代表外部 MinIO 凭据，不再代表集群内 `milvus-minio`。

### 2.2 API / Worker MinIO 配置

`k8s/09-api.yaml` 与 `k8s/10-task-worker.yaml` 应保持以下配置：

```yaml
STORAGE_TYPE: minio
STORAGE_IMPL: MINIO
STORAGE_ENDPOINT: http://172.16.31.63:9000
STORAGE_BUCKET: rag-kb
STORAGE_PREFIX: openrag
STORAGE_PUBLIC_URL: http://172.16.31.63:9000
MILVUS_HOST: milvus
MILVUS_PORT: "19530"
```

部署后验收：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep -E 'STORAGE_|MILVUS_|ELASTICSEARCH'
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep -E 'STORAGE_|MILVUS_|ELASTICSEARCH'
```

期望看到：

```text
STORAGE_ENDPOINT=http://172.16.31.63:9000
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
MILVUS_HOST=milvus
MILVUS_PORT=19530
```

### 2.3 Milvus 外部 MinIO 配置

`k8s/07-milvus.yaml` 中应指向外部 MinIO：

```yaml
MINIO_ADDRESS: 172.16.31.63:9000
```

`k8s/07-milvus-config.yaml` 中应指向：

```yaml
minio:
  address: 172.16.31.63
  port: 9000
  accessKeyID: raguser
  bucketName: rag-kb
  rootPath: milvus
  useSSL: false
```

重要风险点：

- `secretAccessKey` 不能长期保留为字面量占位符。
- 正式部署前要么用部署脚本渲染真实 secret，要么确认 `MINIO_SECRET_ACCESS_KEY` 环境变量能覆盖配置文件。
- 不要把真实 secret 提交到 Git。

部署后验收：

```bash
kubectl -n openrag logs deploy/milvus | grep -Ei 'minio|bucket|root|rag-kb|milvus'
```

期望能确认 Milvus 实际加载：

```text
bucketName = rag-kb
rootPath = milvus
```

### 2.4 Legacy RAGFlow MinIO 配置

`k8s/15-configmap-openrag-service-conf.yaml` 用于兼容旧 RAGFlowMinio 读取 `/app/conf/service_conf.yaml` 的路径。

目标配置：

```yaml
minio:
  host: 172.16.31.63:9000
  user: raguser
  password: "<外部 MinIO secret key>"
  bucket: rag-kb
  prefix_path: openrag
  secure: false
```

注意：

- `password: CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY` 必须在部署前替换。
- 推荐部署时临时渲染 ConfigMap，不建议把真实密码写入仓库文件。

### 2.5 不再部署集群内 milvus-minio

外部 MinIO 模式下，正式部署路径不应执行：

```bash
kubectl apply -f k8s/06-milvus-minio.yaml
```

同时建议从正式 `kustomization` 中移除：

```yaml
- 06-milvus-minio.yaml
```

保留 `06-milvus-minio.yaml` 文件本身没有问题，但它只作为旧 POC/排障/回退参考，不作为本策略的生产依赖。

## 3. 构建机预检查

在 Windows PowerShell 中执行：

```powershell
$repoRoot = "E:\project\OpenRag"
Set-Location $repoRoot

docker info
Get-ChildItem docker, openrag, web, k8s

Get-ChildItem openrag/rag/res/deepdoc |
  Select-String "det.onnx|rec.onnx|ocr.res|layout.onnx|tsr.onnx|updown_concat_xgb.model"
```

如果 `docker info` 报错：

```text
failed to connect to the docker API
```

先启动 Docker Desktop，等待 Docker Engine 正常后再继续。

验收：

- `docker info` 正常输出。
- 当前目录包含 `docker`、`openrag`、`web`、`k8s`。
- Worker 离线模型文件存在。

## 4. 构建业务镜像

设置统一版本号：

```powershell
$env:TAG = "1.0.9"
$env:BUILD_PROXY = "http://http.docker.internal:3128"
$env:NPM_REGISTRY = "https://registry.npmjs.org"
```

构建 API：

```powershell
docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.api `
  -t "openrag/api:$($env:TAG)" .
```

构建 Worker：

```powershell
docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.worker `
  -t "openrag/task-worker:$($env:TAG)" .
```

构建 Web：

```powershell
docker build --build-arg NPM_PROXY=$env:BUILD_PROXY `
  --build-arg NPM_REGISTRY=$env:NPM_REGISTRY `
  -f docker/Dockerfile.web `
  -t "openrag/web:$($env:TAG)" .
```

构建产物：

```text
openrag/api:<TAG>
openrag/task-worker:<TAG>
openrag/web:<TAG>
```

验收：

```powershell
docker image inspect "openrag/api:$($env:TAG)" | Out-Null
docker image inspect "openrag/task-worker:$($env:TAG)" | Out-Null
docker image inspect "openrag/web:$($env:TAG)" | Out-Null
docker images | Select-String "openrag"
```

构建日志建议确认：

```text
openrag.api.main import OK
offline ragflow model bundle OK
task_worker OK
npm run build 成功
```

## 5. 拉取第三方镜像

外部 MinIO 模式下，正式部署不再需要 `minio/minio` 镜像。需要的第三方镜像为：

```powershell
$thirdPartyImages = @(
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2",
  "busybox:1.36"
)

foreach ($image in $thirdPartyImages) {
  docker pull $image
  if ($LASTEXITCODE -ne 0) { throw "镜像拉取失败：$image" }
}
```

验收：

```powershell
docker images | Select-String "postgres|etcd|milvus|elasticsearch|busybox"
```

## 6. 生成离线镜像包

```powershell
$env:OFFLINE_TAR = "openrag-offline-$($env:TAG)-external-minio-images.tar"

$requiredImages = @(
  "postgres:16-alpine",
  "quay.io/coreos/etcd:v3.5.5",
  "milvusdb/milvus:v2.4.17",
  "docker.elastic.co/elasticsearch/elasticsearch:8.12.2",
  "busybox:1.36",
  "openrag/api:$($env:TAG)",
  "openrag/task-worker:$($env:TAG)",
  "openrag/web:$($env:TAG)"
)

foreach ($image in $requiredImages) {
  docker image inspect $image | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "缺少镜像：$image" }
}

docker save -o $env:OFFLINE_TAR $requiredImages

$hash = (Get-FileHash $env:OFFLINE_TAR -Algorithm SHA256).Hash.ToLower()
"$hash  $env:OFFLINE_TAR" | Set-Content "$($env:OFFLINE_TAR).sha256" -Encoding ascii
```

产物：

```text
openrag-offline-1.0.9-external-minio-images.tar
openrag-offline-1.0.9-external-minio-images.tar.sha256
```

验收：

```powershell
Get-ChildItem $env:OFFLINE_TAR, "$($env:OFFLINE_TAR).sha256"
Get-Content "$($env:OFFLINE_TAR).sha256"
```

## 7. 内网制品机导入并推送 Harbor

把离线包复制到内网制品机后执行：

```bash
export TAG=1.0.9
export HARBOR=harbor.internal.example
export HARBOR_PROJECT=openrag

cd /data/openrag-artifacts
sha256sum -c openrag-offline-1.0.9-external-minio-images.tar.sha256
docker load -i openrag-offline-1.0.9-external-minio-images.tar
docker login "$HARBOR"
```

打 tag：

```bash
docker tag postgres:16-alpine $HARBOR/$HARBOR_PROJECT/postgres:16-alpine
docker tag quay.io/coreos/etcd:v3.5.5 $HARBOR/$HARBOR_PROJECT/etcd:v3.5.5
docker tag milvusdb/milvus:v2.4.17 $HARBOR/$HARBOR_PROJECT/milvus:v2.4.17
docker tag docker.elastic.co/elasticsearch/elasticsearch:8.12.2 $HARBOR/$HARBOR_PROJECT/elasticsearch:8.12.2
docker tag busybox:1.36 $HARBOR/$HARBOR_PROJECT/busybox:1.36
docker tag openrag/api:$TAG $HARBOR/$HARBOR_PROJECT/api:$TAG
docker tag openrag/task-worker:$TAG $HARBOR/$HARBOR_PROJECT/task-worker:$TAG
docker tag openrag/web:$TAG $HARBOR/$HARBOR_PROJECT/web:$TAG
```

推送：

```bash
docker push $HARBOR/$HARBOR_PROJECT/postgres:16-alpine
docker push $HARBOR/$HARBOR_PROJECT/etcd:v3.5.5
docker push $HARBOR/$HARBOR_PROJECT/milvus:v2.4.17
docker push $HARBOR/$HARBOR_PROJECT/elasticsearch:8.12.2
docker push $HARBOR/$HARBOR_PROJECT/busybox:1.36
docker push $HARBOR/$HARBOR_PROJECT/api:$TAG
docker push $HARBOR/$HARBOR_PROJECT/task-worker:$TAG
docker push $HARBOR/$HARBOR_PROJECT/web:$TAG
```

验收：

```bash
docker pull $HARBOR/$HARBOR_PROJECT/api:$TAG
docker pull $HARBOR/$HARBOR_PROJECT/task-worker:$TAG
docker pull $HARBOR/$HARBOR_PROJECT/web:$TAG
```

## 8. 部署前验证外部 MinIO

在 K8s master 上执行：

```bash
kubectl apply -f k8s/00-namespace.yaml

kubectl -n openrag run minio-check --rm -it --restart=Never \
  --image=busybox:1.36 \
  -- sh -c 'nc -z 172.16.31.63 9000 && echo minio-ok'
```

如果集群使用内网 Harbor 镜像，把 `busybox:1.36` 换成：

```text
<HARBOR>/<HARBOR_PROJECT>/busybox:1.36
```

使用 `mcli` 验证 bucket：

```bash
mcli alias set new-minio http://172.16.31.63:9000 <access-key> <secret-key>
mcli ls new-minio/rag-kb
mcli ls new-minio/rag-kb/openrag
mcli ls new-minio/rag-kb/milvus-backups
```

如果已经搬运 Milvus backup，继续验证：

```bash
export TS=20260519174905
mcli ls --recursive new-minio/rag-kb/milvus-backups/$TS | head -20
```

## 9. 部署 Kubernetes 基础配置

创建 Namespace、Secret、ConfigMap：

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-secret.yaml
kubectl apply -f k8s/02-configmap-nginx.yaml
kubectl apply -f k8s/13-configmap-openrag-llm.yaml
kubectl apply -f k8s/14-configmap-openrag-web-runtime.yaml
kubectl apply -f k8s/15-configmap-openrag-service-conf.yaml
```

验收：

```bash
kubectl -n openrag get secret,cm
```

如果 Harbor 需要认证：

```bash
kubectl -n openrag create secret docker-registry harbor-regcred \
  --docker-server="$HARBOR" \
  --docker-username="YOUR_USERNAME" \
  --docker-password="YOUR_PASSWORD_OR_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n openrag patch serviceaccount default \
  -p '{"imagePullSecrets":[{"name":"harbor-regcred"}]}'
```

验收：

```bash
kubectl -n openrag get serviceaccount default -o yaml | grep -A3 imagePullSecrets
```

## 10. 部署有状态与中间件组件

按顺序执行：

```bash
kubectl apply -f k8s/03-postgres.yaml
kubectl apply -f k8s/05-milvus-etcd.yaml
kubectl apply -f k8s/07-milvus-config.yaml
kubectl apply -f k8s/07-milvus.yaml
kubectl apply -f k8s/08-elasticsearch.yaml
```

不要执行：

```bash
kubectl apply -f k8s/06-milvus-minio.yaml
```

等待组件 Ready：

```bash
kubectl -n openrag rollout status statefulset/postgres --timeout=600s
kubectl -n openrag rollout status deployment/milvus-etcd --timeout=600s
kubectl -n openrag rollout status deployment/milvus --timeout=600s
kubectl -n openrag rollout status statefulset/elasticsearch --timeout=600s
```

连通性验收：

```bash
kubectl -n openrag run netcheck --rm -it --restart=Never \
  --image=busybox:1.36 \
  -- sh -c 'nc -z postgres 5432 && nc -z elasticsearch 9200 && nc -z milvus 19530 && nc -z 172.16.31.63 9000 && echo ok'
```

## 11. 数据恢复策略

如果是全新空环境，可以跳过本节。

如果是承接旧环境数据，必须在启动 API/Worker 正式服务前完成：

```text
1. Postgres restore。
2. 旧 MinIO 业务对象已迁移到 rag-kb/openrag/。
3. Milvus backup restore 到新 Milvus 的 rag-kb/milvus/。
```

Milvus restore 路径约定：

```text
backup 源: rag-kb/milvus-backups/20260519174905
restore 目标: rag-kb/milvus
```

恢复后验收 collection entity count：

```bash
kubectl -n openrag exec deploy/openrag-api -- python - <<'PY'
from pymilvus import connections, utility, Collection
connections.connect(host="milvus", port="19530")
for name in utility.list_collections():
    c = Collection(name)
    print(name, c.num_entities)
PY
```

历史基线参考：

```text
openrag_chunks 47979
openrag_layers 3022
```

## 12. 部署业务组件

```bash
kubectl apply -f k8s/09-api.yaml
kubectl apply -f k8s/10-task-worker.yaml
kubectl apply -f k8s/11-web.yaml
kubectl apply -f k8s/12-ingress.yaml
```

等待 Ready：

```bash
kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
kubectl -n openrag rollout status deployment/openrag-task-worker --timeout=600s
kubectl -n openrag rollout status deployment/openrag-web --timeout=600s
```

验收：

```bash
kubectl -n openrag get pods -o wide
kubectl -n openrag get svc,ingress,pvc
```

## 13. 部署后验证

检查环境变量：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep -E 'STORAGE_|MILVUS_|ELASTICSEARCH'
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep -E 'STORAGE_|MILVUS_|ELASTICSEARCH'
```

检查 Milvus 对象存储配置：

```bash
kubectl -n openrag logs deploy/milvus | grep -Ei 'minio|bucket|root|rag-kb|milvus'
```

API 健康检查：

```bash
kubectl -n openrag port-forward svc/api 8000:8000
curl -sS http://127.0.0.1:8000/health
```

Web 健康检查：

```bash
kubectl -n openrag port-forward svc/web 8080:80
curl -I http://127.0.0.1:8080
curl -sS http://127.0.0.1:8080/api/health
```

Postgres 验收：

```bash
kubectl -n openrag exec statefulset/postgres -- \
  pg_isready -U openrag -d openrag

kubectl -n openrag exec statefulset/postgres -- \
  psql -U openrag -d openrag -c "\dt"
```

MinIO 路径验收：

```bash
mcli ls new-minio/rag-kb/openrag
mcli ls new-minio/rag-kb/milvus
```

业务链路验收：

```text
1. 打开 Web 页面。
2. 登录。
3. 查看已有 workspace 和文件是否正常。
4. 上传一个小文件。
5. 观察 openrag-task-worker 日志，确认任务被消费。
6. 在 new-minio/rag-kb/openrag/ 下确认新增对象。
7. 执行一次检索或问答。
8. 确认 Milvus、ES、Postgres 无持续异常日志。
```

## 14. 最终成功标准

最终上线成功需要同时满足：

```text
1. 不再依赖 K8s 内部 milvus-minio。
2. API / Worker 的 STORAGE_* 全部指向外部 MinIO。
3. Milvus 的 bucketName/rootPath 是 rag-kb/milvus。
4. OpenRag 业务对象在 rag-kb/openrag/ 下。
5. Milvus 对象在 rag-kb/milvus/ 下。
6. Pods 全部 Running / Ready。
7. PVC 全部 Bound。
8. /health 正常。
9. Web 可访问。
10. 上传、解析、入库、检索完整链路成功。
```

## 15. 常见问题

### 15.1 Docker API 连接失败

现象：

```text
failed to connect to the docker API
```

处理：

```powershell
docker info
```

如果仍失败，启动 Docker Desktop，等待 Docker Engine 正常后重新执行。

### 15.2 PowerShell 续行符错误

不要把 Bash 的 `\` 复制到 PowerShell。PowerShell 使用反引号：

```powershell
docker build `
  -f docker/Dockerfile.api `
  -t "openrag/api:$($env:TAG)" .
```

### 15.3 K8s ImagePullBackOff

排查：

```bash
kubectl -n openrag describe pod <pod-name>
```

重点检查：

```text
1. 镜像是否仍指向公网。
2. busybox 是否已推送 Harbor。
3. imagePullSecrets 是否配置。
4. 节点是否能访问 Harbor。
```

### 15.4 Milvus 仍访问旧 MinIO

排查：

```bash
kubectl -n openrag exec deploy/milvus -- env | grep MINIO
kubectl -n openrag logs deploy/milvus | grep -Ei 'minio|bucket|root'
```

如果看到 `milvus-minio:9000`、`a-bucket` 或 `files`，说明仍在使用旧配置，需要重新检查：

```text
k8s/07-milvus.yaml
k8s/07-milvus-config.yaml
k8s/kustomization.yaml
```

### 15.5 API / Worker 文件仍写入旧路径

排查：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep STORAGE
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep STORAGE
mcli ls new-minio/rag-kb/openrag
```

如果 `STORAGE_BUCKET` 不是 `rag-kb`，或 `STORAGE_PREFIX` 不是 `openrag`，说明部署清单不是迁移后的版本。
