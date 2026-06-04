# OpenRag 内网 Kubernetes 离线部署攻略

本文用于指导 OpenRag 在**内网 Kubernetes 集群**中的离线部署。默认部署口径如下：

- 内网 Kubernetes 节点不能访问外部网络。
- 默认**没有内网 Harbor 镜像仓库**，镜像通过 `docker save` / `docker load` 或 `ctr images import` 导入到每个 K8s 节点。
- 不直接部署根目录 `k8s/`，而是先在外网构建机运行 `scripts/render-k8s-profile.ps1`，生成 `k8s-rendered/internal/` 后再部署。
- 对象存储使用外部 MinIO：`http://172.16.31.63:9000`。
- OpenRag 业务文件使用单 bucket：`rag-kb/openrag/`。
- Milvus 向量对象使用同一个 bucket 下的独立路径：`rag-kb/milvus/`。
- 不部署 `k8s/06-milvus-minio.yaml`。
- 内网模型网关通过 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSION`、`L1_NAV_MODEL` 配置。

如果你的内网已经有 Harbor，可以参考本文最后的“可选：使用内网 Harbor”章节；否则按主流程执行。

## 0. 部署前必须确认的信息

部署前先准备这些值：

| 名称 | 示例 | 用途 |
| --- | --- | --- |
| `TAG` | `1.0.9` | 本次部署镜像版本号 |
| `MINIO_ENDPOINT` | `http://172.16.31.63:9000` | API / Worker 访问 MinIO |
| `MINIO_ADDRESS` | `172.16.31.63` | Milvus 配置中的 MinIO 地址 |
| `MINIO_PORT` | `9000` | Milvus 配置中的 MinIO 端口 |
| `MINIO_BUCKET` | `rag-kb` | 单 bucket 名称 |
| `OPENRAG_PREFIX` | `openrag` | OpenRag 业务文件前缀 |
| `MILVUS_ROOT_PATH` | `milvus` | Milvus 向量对象前缀 |
| `OPENAI_BASE_URL` | `http://litellm.dev.guozhijishu.com/v1` | 内网模型网关地址 |
| `EMBEDDING_MODEL` | `Qwen3-Embedding-4B` | embedding 模型 |
| `EMBEDDING_DIMENSION` | `2560` | Qwen3-Embedding-4B 向量维度 |
| `L1_NAV_MODEL` | `DeepSeek-V4-Flash` | LLM 模型 |

验收标准：

```bash
# 你应该能明确回答这些问题：
# 1. 本次 TAG 是多少？
# 2. 每个 K8s 节点是否都能导入离线镜像？
# 3. K8s 集群是否能访问 172.16.31.63:9000？
# 4. rag-kb bucket 是否存在？
# 5. 内网模型网关地址和 api_key 是否已经准备好？
```

## 1. 外网构建机准备

以下命令在外网 Windows 构建机执行，默认项目目录为 `E:\project\OpenRag`。

```powershell
$ErrorActionPreference = "Stop"
$repoRoot = "E:\project\OpenRag"
Set-Location $repoRoot

$env:TAG = "1.0.9"
$artifactDir = Join-Path $repoRoot "artifacts"
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
```

检查工作区：

```powershell
git status --short
Get-ChildItem docker, openrag, web, k8s, deploy, scripts
```

验收标准：

- 当前目录是 `E:\project\OpenRag`。
- 能看到 `docker/`、`openrag/`、`web/`、`k8s/`、`deploy/`、`scripts/`。
- `git status --short` 中没有你不认识的待提交改动。若有，需要先确认这些改动是否应该进入本次镜像。

## 2. 检查内网 profile 和模型配置

检查内网 MinIO profile：

```powershell
Get-Content deploy\envs\internal.yaml
```

期望包含：

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

检查内网模型配置：

```powershell
Get-Content k8s\13-configmap-openrag-llm.yaml
```

期望包含：

```yaml
OPENAI_BASE_URL: "http://litellm.dev.guozhijishu.com/v1"
EMBEDDING_MODEL: "Qwen3-Embedding-4B"
EMBEDDING_DIMENSION: "2560"
L1_NAV_MODEL: "DeepSeek-V4-Flash"
```

如果内网模型网关地址不是 `http://litellm.dev.guozhijishu.com/v1`，先修改 `k8s/13-configmap-openrag-llm.yaml`，再继续后续步骤。

验收标准：

```powershell
Select-String -Path deploy\envs\internal.yaml -Pattern "172.16.31.63|rag-kb|openrag|milvus"
Select-String -Path k8s\13-configmap-openrag-llm.yaml -Pattern "OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
```

输出中必须能看到 MinIO、bucket、prefix、embedding 维度和模型网关配置。

## 3. 渲染内网 Kubernetes 清单

先运行渲染脚本自测：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-render-k8s-profile.ps1
```

验收标准：

```text
render-k8s-profile tests passed
```

生成内网部署清单：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\render-k8s-profile.ps1 -Profile internal -Tag $env:TAG
```

验收标准：

```powershell
Get-ChildItem k8s-rendered\internal
Select-String -Path k8s-rendered\internal\09-api.yaml,k8s-rendered\internal\10-task-worker.yaml,k8s-rendered\internal\11-web.yaml -Pattern "STORAGE_ENDPOINT|STORAGE_BUCKET|STORAGE_PREFIX|OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL|image:|imagePullPolicy"
Select-String -Path k8s-rendered\internal\07-milvus.yaml,k8s-rendered\internal\07-milvus-config.yaml -Pattern "MINIO_ADDRESS|172.16.31.63|rag-kb|rootPath"
Select-String -Path k8s-rendered\internal\kustomization.yaml -Pattern "06-milvus-minio"
```

期望结果：

- `k8s-rendered/internal/` 目录存在。
- API 镜像是 `openrag/api:<TAG>`。
- Worker 镜像是 `openrag/task-worker:<TAG>`。
- Web 镜像是 `openrag/web:<TAG>`。
- API / Worker / Web 的 `imagePullPolicy` 是 `IfNotPresent`。
- API / Worker 的 `STORAGE_ENDPOINT` 是 `http://172.16.31.63:9000`。
- API / Worker 的 `STORAGE_BUCKET` 是 `rag-kb`。
- API / Worker 的 `STORAGE_PREFIX` 是 `openrag`。
- Milvus 的 `MINIO_ADDRESS` 是 `172.16.31.63:9000`。
- Milvus 的 `bucketName` 是 `rag-kb`。
- Milvus 的 `rootPath` 是 `milvus`。
- 最后一条 `Select-String` 不应输出 `06-milvus-minio`。

不要手工维护 `k8s-rendered/internal/`。如果配置或 tag 变化，重新运行渲染脚本。

## 4. 构建业务镜像

确认 Docker Engine 可用：

```powershell
docker info
```

如果需要代理才能访问 npm、pip、Debian 源，设置代理；如果不需要代理，可以跳过这几行：

```powershell
$env:BUILD_PROXY = "http://http.docker.internal:3128"
$env:NPM_REGISTRY = "https://registry.npmjs.org"
```

构建 API 镜像：

```powershell
docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.api `
  -t "openrag/api:$($env:TAG)" .
```

构建 Worker 镜像：

```powershell
docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY `
  -f docker/Dockerfile.worker `
  -t "openrag/task-worker:$($env:TAG)" .
```

构建 Web 镜像：

```powershell
docker build --build-arg NPM_PROXY=$env:BUILD_PROXY `
  --build-arg NPM_REGISTRY=$env:NPM_REGISTRY `
  -f docker/Dockerfile.web `
  -t "openrag/web:$($env:TAG)" .
```

验收标准：

```powershell
docker image inspect "openrag/api:$($env:TAG)" | Out-Null
docker image inspect "openrag/task-worker:$($env:TAG)" | Out-Null
docker image inspect "openrag/web:$($env:TAG)" | Out-Null
docker images | Select-String "openrag"
```

三条 `docker image inspect` 都不能报错，`docker images` 中必须能看到本次 tag。

## 5. 拉取第三方镜像

内网不能访问外部网络，所以第三方镜像也必须在外网构建机提前拉取并打进离线包。

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
  if ($LASTEXITCODE -ne 0) {
    throw "镜像拉取失败: $image"
  }
}
```

验收标准：

```powershell
docker images | Select-String "postgres|etcd|milvus|elasticsearch|busybox"
```

输出中必须包含 5 个第三方镜像。

## 6. 生成离线部署制品

生成镜像 tar 包：

```powershell
$imageTar = Join-Path $artifactDir "openrag-k8s-images-$($env:TAG).tar"

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
  if ($LASTEXITCODE -ne 0) {
    throw "缺少镜像: $image"
  }
}

docker save -o $imageTar $requiredImages
```

生成 sha256 校验文件：

```powershell
$hash = (Get-FileHash $imageTar -Algorithm SHA256).Hash.ToLower()
"$hash  $(Split-Path $imageTar -Leaf)" | Set-Content "$imageTar.sha256" -Encoding ascii
```

打包渲染后的 K8s 清单：

```powershell
$manifestZip = Join-Path $artifactDir "openrag-k8s-rendered-$($env:TAG)-internal.zip"
if (Test-Path $manifestZip) {
  Remove-Item $manifestZip -Force
}
Compress-Archive -Path "k8s-rendered" -DestinationPath $manifestZip
```

生成制品清单：

```powershell
$manifestText = Join-Path $artifactDir "openrag-k8s-$($env:TAG)-manifest.txt"
@(
  "TAG=$($env:TAG)",
  "IMAGE_TAR=$(Split-Path $imageTar -Leaf)",
  "IMAGE_TAR_SHA256=$(Split-Path $imageTar -Leaf).sha256",
  "K8S_RENDERED_ZIP=$(Split-Path $manifestZip -Leaf)",
  "MINIO_ENDPOINT=http://172.16.31.63:9000",
  "MINIO_BUCKET=rag-kb",
  "OPENRAG_PREFIX=openrag",
  "MILVUS_ROOT_PATH=milvus",
  "OPENAI_BASE_URL=http://litellm.dev.guozhijishu.com/v1",
  "EMBEDDING_MODEL=Qwen3-Embedding-4B",
  "EMBEDDING_DIMENSION=2560",
  "L1_NAV_MODEL=DeepSeek-V4-Flash"
) | Set-Content $manifestText -Encoding utf8
```

验收标准：

```powershell
Get-ChildItem $artifactDir | Select-String "openrag-k8s"
Get-Content "$imageTar.sha256"
Get-Content $manifestText
```

必须得到以下 4 类文件：

```text
openrag-k8s-images-<TAG>.tar
openrag-k8s-images-<TAG>.tar.sha256
openrag-k8s-rendered-<TAG>-internal.zip
openrag-k8s-<TAG>-manifest.txt
```

## 7. 将制品带入内网

把以下文件带入内网 K8s 控制节点：

```text
openrag-k8s-images-<TAG>.tar
openrag-k8s-images-<TAG>.tar.sha256
openrag-k8s-rendered-<TAG>-internal.zip
openrag-k8s-<TAG>-manifest.txt
```

如果可以从外网构建机 scp 到内网控制节点，可执行：

```powershell
$target = "user@<K8S_CONTROL_PLANE_IP>:/data/openrag-deploy/artifacts/"
scp "$artifactDir\openrag-k8s-images-$($env:TAG).tar" $target
scp "$artifactDir\openrag-k8s-images-$($env:TAG).tar.sha256" $target
scp "$artifactDir\openrag-k8s-rendered-$($env:TAG)-internal.zip" $target
scp "$artifactDir\openrag-k8s-$($env:TAG)-manifest.txt" $target
```

如果不能 scp，使用 U 盘、共享目录、堡垒机文件上传等方式。不要把真实 Secret 写进这些制品。

验收标准，在内网控制节点执行：

```bash
export TAG=1.0.9
export WORKDIR=/data/openrag-deploy
export ARTIFACT_DIR=$WORKDIR/artifacts

mkdir -p "$ARTIFACT_DIR"
ls -lh "$ARTIFACT_DIR"
cat "$ARTIFACT_DIR/openrag-k8s-$TAG-manifest.txt"
```

必须能看到 4 个制品文件。

## 8. 内网控制节点校验制品

在内网控制节点执行：

```bash
cd "$ARTIFACT_DIR"
sha256sum -c "openrag-k8s-images-$TAG.tar.sha256"
```

验收标准：

```text
openrag-k8s-images-<TAG>.tar: OK
```

解压 K8s 清单：

```bash
cd "$WORKDIR"
rm -rf k8s-rendered
unzip -q "$ARTIFACT_DIR/openrag-k8s-rendered-$TAG-internal.zip" -d "$WORKDIR"
```

如果内网没有 `unzip`，请先在外网把 zip 解压成目录后再传入内网。

验收标准：

```bash
export MANIFEST_DIR=$WORKDIR/k8s-rendered/internal

test -d "$MANIFEST_DIR"
ls -lh "$MANIFEST_DIR"
grep -R "openrag/api:$TAG" "$MANIFEST_DIR/09-api.yaml"
grep -R "openrag/task-worker:$TAG" "$MANIFEST_DIR/10-task-worker.yaml"
grep -R "openrag/web:$TAG" "$MANIFEST_DIR/11-web.yaml"
grep -R "http://172.16.31.63:9000" "$MANIFEST_DIR/09-api.yaml" "$MANIFEST_DIR/10-task-worker.yaml"
grep -R "bucketName: rag-kb" "$MANIFEST_DIR/07-milvus-config.yaml"
grep -R "rootPath: milvus" "$MANIFEST_DIR/07-milvus-config.yaml"
```

全部命令都应有输出，且 tag 必须等于本次部署的 `TAG`。

## 9. 将镜像导入每个 K8s 节点

先查看集群节点：

```bash
kubectl get nodes -o wide
kubectl describe node <NODE_NAME> | grep -i "Container Runtime Version"
```

把镜像 tar 包复制到每个可能运行 Pod 的节点。示例：

```bash
scp "$ARTIFACT_DIR/openrag-k8s-images-$TAG.tar" user@<NODE_1>:/data/openrag-deploy/
scp "$ARTIFACT_DIR/openrag-k8s-images-$TAG.tar" user@<NODE_2>:/data/openrag-deploy/
scp "$ARTIFACT_DIR/openrag-k8s-images-$TAG.tar" user@<NODE_3>:/data/openrag-deploy/
```

如果节点使用 Docker runtime，在每个节点执行：

```bash
sudo docker load -i /data/openrag-deploy/openrag-k8s-images-$TAG.tar
sudo docker images | grep -E "openrag|postgres|etcd|milvus|elasticsearch|busybox"
```

如果节点使用 containerd runtime，在每个节点执行：

```bash
sudo ctr -n k8s.io images import /data/openrag-deploy/openrag-k8s-images-$TAG.tar
sudo ctr -n k8s.io images ls | grep -E "openrag|postgres|etcd|milvus|elasticsearch|busybox"
```

如果节点安装了 `crictl`，也可以验收：

```bash
sudo crictl images | grep -E "openrag|postgres|etcd|milvus|elasticsearch|busybox"
```

验收标准：

每个 K8s 节点上都必须能看到这些镜像：

```text
postgres:16-alpine
quay.io/coreos/etcd:v3.5.5
milvusdb/milvus:v2.4.17
docker.elastic.co/elasticsearch/elasticsearch:8.12.2
busybox:1.36
openrag/api:<TAG>
openrag/task-worker:<TAG>
openrag/web:<TAG>
```

注意：只在控制节点导入镜像是不够的。Pod 调度到没有镜像的工作节点时，会出现 `ImagePullBackOff`。

## 10. 检查 Kubernetes 基础能力

在内网控制节点执行：

```bash
kubectl version --short
kubectl get nodes
kubectl get storageclass
kubectl get ingressclass
```

验收标准：

- 所有预期节点都是 `Ready`。
- 至少有一个可用的 `StorageClass`，否则 Postgres、Milvus、Elasticsearch 的 PVC 无法绑定。
- 如果要通过 Ingress 访问 Web，必须存在 `nginx` 或与你的 `12-ingress.yaml` 匹配的 IngressClass。
- 如果没有 Ingress Controller，后续可以先用 `kubectl port-forward` 验收。

## 11. 检查外部 MinIO 连通性

先创建 namespace：

```bash
kubectl apply -f "$MANIFEST_DIR/00-namespace.yaml"
kubectl get ns openrag
```

用 busybox 检查 K8s Pod 到 MinIO 的网络：

```bash
kubectl -n openrag run minio-check --rm -it --restart=Never \
  --image=busybox:1.36 \
  --image-pull-policy=IfNotPresent \
  -- sh -c 'nc -z 172.16.31.63 9000 && echo minio-ok'
```

验收标准：

```text
minio-ok
```

如果内网机器有 `mcli`，继续检查 bucket：

```bash
mcli alias set new-minio http://172.16.31.63:9000 <MINIO_ACCESS_KEY> <MINIO_SECRET_KEY>
mcli ls new-minio/rag-kb
mcli ls new-minio/rag-kb/openrag
mcli ls new-minio/rag-kb/milvus
```

验收标准：

- `rag-kb` bucket 存在。
- `rag-kb/openrag` 用于 OpenRag 业务文件。
- `rag-kb/milvus` 用于 Milvus 向量对象。

如果 `rag-kb/openrag` 或 `rag-kb/milvus` 不存在，但 bucket 存在，可以根据 MinIO 策略先创建前缀目录或上传占位对象。

## 12. 准备并应用真实 Secret

复制 Secret 示例：

```bash
cp "$MANIFEST_DIR/01-secret.example.yaml" "$MANIFEST_DIR/01-secret.yaml"
chmod 600 "$MANIFEST_DIR/01-secret.yaml"
vi "$MANIFEST_DIR/01-secret.yaml"
```

必须填写：

```text
POSTGRES_PASSWORD
SECRET_KEY
MINIO_ROOT_USER
MINIO_ROOT_PASSWORD
MILVUS_SECRET_KEY
OPENAI_API_KEY
```

填写说明：

- `POSTGRES_PASSWORD`：Postgres 密码。
- `SECRET_KEY`：JWT 密钥，建议至少 32 个字符。
- `MINIO_ROOT_USER`：外部 MinIO access key。
- `MINIO_ROOT_PASSWORD`：外部 MinIO secret key。
- `MILVUS_SECRET_KEY`：通常填同一个外部 MinIO secret key。
- `OPENAI_API_KEY`：内网模型网关需要的 key。如果网关只要求占位，也要填网关认可的值。

不要把真实 `01-secret.yaml` 带回外网，也不要提交到 Git。

检查是否还有占位符：

```bash
grep -n "CHANGE_ME" "$MANIFEST_DIR/01-secret.yaml" || true
grep -n 'OPENAI_API_KEY: ""' "$MANIFEST_DIR/01-secret.yaml" || true
```

验收标准：

- 不应再出现 `CHANGE_ME`。
- `OPENAI_API_KEY` 不应为空。

应用 Secret：

```bash
kubectl apply -f "$MANIFEST_DIR/01-secret.yaml"
```

验收 Secret 键是否存在，不要打印真实值：

```bash
kubectl -n openrag get secret openrag-secrets -o yaml | grep -E "POSTGRES_PASSWORD|SECRET_KEY|MINIO_ROOT_USER|MINIO_ROOT_PASSWORD|MILVUS_SECRET_KEY|OPENAI_API_KEY"
```

验收标准：

输出中能看到 6 个 key。

## 13. 应用基础 ConfigMap

执行：

```bash
kubectl apply -f "$MANIFEST_DIR/02-configmap-nginx.yaml"
kubectl apply -f "$MANIFEST_DIR/13-configmap-openrag-llm.yaml"
kubectl apply -f "$MANIFEST_DIR/14-configmap-openrag-web-runtime.yaml"
kubectl apply -f "$MANIFEST_DIR/15-configmap-openrag-service-conf.yaml"
```

验收：

```bash
kubectl -n openrag get cm
kubectl -n openrag get cm openrag-llm-config -o yaml | grep -E "OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
kubectl -n openrag get cm openrag-service-conf -o yaml | grep -E "172.16.31.63:9000|rag-kb|openrag"
```

期望：

- `OPENAI_BASE_URL` 是内网模型网关地址。
- `EMBEDDING_MODEL` 是 `Qwen3-Embedding-4B`。
- `EMBEDDING_DIMENSION` 是 `2560`。
- `L1_NAV_MODEL` 是 `DeepSeek-V4-Flash`。
- `service_conf.yaml` 中 MinIO 指向 `172.16.31.63:9000`、`rag-kb`、`openrag`。

## 14. 部署 Postgres

执行：

```bash
kubectl apply -f "$MANIFEST_DIR/03-postgres.yaml"
kubectl -n openrag rollout status statefulset/postgres --timeout=600s
```

验收：

```bash
kubectl -n openrag get pod -l app=postgres -o wide
kubectl -n openrag get pvc
kubectl -n openrag exec statefulset/postgres -- pg_isready -U openrag -d openrag
```

期望：

- Postgres Pod 是 `Running`。
- Postgres PVC 是 `Bound`。
- `pg_isready` 输出 `accepting connections`。

## 15. 部署 Milvus etcd

执行：

```bash
kubectl apply -f "$MANIFEST_DIR/05-milvus-etcd.yaml"
kubectl -n openrag rollout status deployment/milvus-etcd --timeout=600s
```

验收：

```bash
kubectl -n openrag get pod -l app=milvus-etcd -o wide
kubectl -n openrag get pvc | grep milvus-etcd
kubectl -n openrag logs deploy/milvus-etcd --tail=50
```

期望：

- etcd Pod 是 `Running`。
- etcd PVC 是 `Bound`。
- 日志中没有持续重启或明显错误。

## 16. 部署 Milvus

先应用 Milvus ConfigMap：

```bash
kubectl apply -f "$MANIFEST_DIR/07-milvus-config.yaml"
```

检查 ConfigMap：

```bash
kubectl -n openrag get cm milvus-config -o yaml | grep -E "address: 172.16.31.63|bucketName: rag-kb|rootPath: milvus|secretAccessKey"
```

期望：

- `address: 172.16.31.63`
- `bucketName: rag-kb`
- `rootPath: milvus`
- `secretAccessKey` 仍是占位符也可以，因为 Milvus Deployment 会通过环境变量读取 Secret。

部署 Milvus：

```bash
kubectl apply -f "$MANIFEST_DIR/07-milvus.yaml"
kubectl -n openrag rollout status deployment/milvus --timeout=600s
```

验收：

```bash
kubectl -n openrag get pod -l app=milvus -o wide
kubectl -n openrag exec deploy/milvus -- env | grep MINIO
kubectl -n openrag logs deploy/milvus --tail=200 | grep -Ei "minio|bucket|root|rag-kb|milvus" || true
```

期望：

- Milvus Pod 是 `Running`。
- `MINIO_ADDRESS=172.16.31.63:9000`。
- Milvus 日志能体现加载了外部 MinIO、`rag-kb`、`milvus`。

## 17. 部署 Elasticsearch

执行：

```bash
kubectl apply -f "$MANIFEST_DIR/08-elasticsearch.yaml"
kubectl -n openrag rollout status statefulset/elasticsearch --timeout=600s
```

验收：

```bash
kubectl -n openrag get pod -l app=elasticsearch -o wide
kubectl -n openrag get pvc | grep elasticsearch
kubectl -n openrag run es-check --rm -it --restart=Never \
  --image=busybox:1.36 \
  --image-pull-policy=IfNotPresent \
  -- wget -qO- http://elasticsearch:9200
```

期望：

- Elasticsearch Pod 是 `Running`。
- Elasticsearch PVC 是 `Bound`。
- `curl` 返回 Elasticsearch JSON 信息。

## 18. 验证中间件连通性

执行：

```bash
kubectl -n openrag run netcheck --rm -it --restart=Never \
  --image=busybox:1.36 \
  --image-pull-policy=IfNotPresent \
  -- sh -c 'nc -z postgres 5432 && nc -z elasticsearch 9200 && nc -z milvus 19530 && nc -z 172.16.31.63 9000 && echo deps-ok'
```

验收标准：

```text
deps-ok
```

如果这里失败，不要继续部署 API / Worker，先修网络、Service、Pod 或 MinIO 连通性。

## 19. 数据恢复策略

如果是全新空环境，可以跳过本节。

如果是承接旧环境数据，必须在启动 API / Worker 正式服务前完成：

```text
1. Postgres 数据恢复。
2. 旧 MinIO 业务对象迁移到 rag-kb/openrag/。
3. Milvus backup restore 到 rag-kb/milvus/。
```

Postgres 恢复示例：

```bash
kubectl -n openrag cp /data/backup/openrag-postgres.sql postgres-0:/tmp/openrag-postgres.sql
kubectl -n openrag exec statefulset/postgres -- \
  psql -U openrag -d openrag -f /tmp/openrag-postgres.sql
```

Milvus collection 数量验收示例：

```bash
kubectl -n openrag run pymilvus-check --rm -it --restart=Never \
  --image=openrag/api:$TAG \
  --image-pull-policy=IfNotPresent \
  -- python - <<'PY'
from pymilvus import connections, utility, Collection
connections.connect(host="milvus", port="19530")
for name in utility.list_collections():
    c = Collection(name)
    print(name, c.num_entities)
PY
```

历史基线可参考：

```text
openrag_chunks 47979
openrag_layers 3022
```

验收标准：

- Postgres 中能看到业务表。
- MinIO 中旧业务对象已经位于 `rag-kb/openrag/`。
- Milvus collection 存在且 entity 数量符合预期。

## 20. 部署 API

执行：

```bash
kubectl apply -f "$MANIFEST_DIR/09-api.yaml"
kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
```

验收 Pod：

```bash
kubectl -n openrag get pod -l app=openrag-api -o wide
kubectl -n openrag logs deploy/openrag-api --tail=100
```

验收环境变量：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep -E "STORAGE_|MILVUS_|ELASTICSEARCH|OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
```

期望：

```text
STORAGE_ENDPOINT=http://172.16.31.63:9000
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
STORAGE_PUBLIC_URL=http://172.16.31.63:9000
MILVUS_HOST=milvus
MILVUS_PORT=19530
OPENAI_BASE_URL=http://litellm.dev.guozhijishu.com/v1
EMBEDDING_MODEL=Qwen3-Embedding-4B
EMBEDDING_DIMENSION=2560
L1_NAV_MODEL=DeepSeek-V4-Flash
```

API 健康检查：

```bash
kubectl -n openrag port-forward svc/api 8000:8000
```

另开一个终端执行：

```bash
curl -sS http://127.0.0.1:8000/health
```

验收标准：

- API Pod 是 `Running` / `Ready`。
- 日志没有持续异常。
- `/health` 返回健康结果。

## 21. 部署 Worker

执行：

```bash
kubectl apply -f "$MANIFEST_DIR/10-task-worker.yaml"
kubectl -n openrag rollout status deployment/openrag-task-worker --timeout=600s
```

验收 Pod：

```bash
kubectl -n openrag get pod -l app=openrag-task-worker -o wide
kubectl -n openrag logs deploy/openrag-task-worker --tail=100
```

验收环境变量：

```bash
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep -E "STORAGE_|MILVUS_|ELASTICSEARCH|OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
```

期望与 API 一致：

```text
STORAGE_ENDPOINT=http://172.16.31.63:9000
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
STORAGE_PUBLIC_URL=http://172.16.31.63:9000
MILVUS_HOST=milvus
MILVUS_PORT=19530
OPENAI_BASE_URL=http://litellm.dev.guozhijishu.com/v1
EMBEDDING_MODEL=Qwen3-Embedding-4B
EMBEDDING_DIMENSION=2560
L1_NAV_MODEL=DeepSeek-V4-Flash
```

说明：

- `OPENRAG_CHUNK_SIZE` 和 `OPENRAG_CHUNK_OVERLAP` 当前不需要在 K8s 中显式配置。
- 代码默认值是 `600/80`。
- 如果未来要不改镜像就调整 chunk 参数，再在 API / Worker manifest 中显式加环境变量。

## 22. 部署 Web

执行：

```bash
kubectl apply -f "$MANIFEST_DIR/11-web.yaml"
kubectl -n openrag rollout status deployment/openrag-web --timeout=600s
```

验收 Pod：

```bash
kubectl -n openrag get pod -l app=openrag-web -o wide
kubectl -n openrag logs deploy/openrag-web --tail=100
```

Web 健康检查：

```bash
kubectl -n openrag port-forward svc/web 8080:80
```

另开一个终端执行：

```bash
curl -I http://127.0.0.1:8080
curl -sS http://127.0.0.1:8080/app-config.js
curl -sS http://127.0.0.1:8080/api/health
```

验收标准：

- `curl -I` 返回 `200` 或 `30x`。
- `/app-config.js` 返回前端运行时配置。
- `/api/health` 能通过 Web Nginx 代理到 API。

## 23. 部署 Ingress

如果集群已有 `ingress-nginx`，执行：

```bash
kubectl apply -f "$MANIFEST_DIR/12-ingress.yaml"
kubectl -n openrag get ingress
kubectl -n openrag describe ingress openrag
```

验收标准：

- Ingress 对象存在。
- `ADDRESS` 不为空，或者 Ingress Controller 日志中能看到规则已加载。
- 访问域名能进入 Web。

如果没有 Ingress Controller，不要卡在这一步。可以暂时用：

```bash
kubectl -n openrag port-forward svc/web 8080:80
```

然后访问：

```text
http://127.0.0.1:8080
```

## 24. 一次性补齐 apply

如果前面按文件分阶段部署完成，本节通常不需要执行。若你想让 Kustomize 补齐所有资源，可以执行：

```bash
kubectl apply -k "$MANIFEST_DIR"
```

验收：

```bash
kubectl -n openrag get pods,svc,ingress,pvc
```

注意：

- 真实 Secret 不在 `kustomization.yaml` 中，仍需单独 `kubectl apply -f "$MANIFEST_DIR/01-secret.yaml"`。
- 不要执行 `kubectl apply -f "$MANIFEST_DIR/06-milvus-minio.yaml"`。

## 25. 最终技术验收

检查所有工作负载：

```bash
kubectl -n openrag get pods -o wide
kubectl -n openrag get deploy,statefulset,svc,ingress,pvc
```

期望：

- 所有 Pod 是 `Running`。
- API、Worker、Web Deployment 是 Ready。
- Postgres、Elasticsearch StatefulSet 是 Ready。
- 所有 PVC 是 `Bound`。

检查镜像没有走外部拉取：

```bash
kubectl -n openrag describe pod -l app=openrag-api | grep -Ei "Image:|Image ID:|Pulling|Pulled|Failed"
kubectl -n openrag describe pod -l app=openrag-task-worker | grep -Ei "Image:|Image ID:|Pulling|Pulled|Failed"
kubectl -n openrag describe pod -l app=openrag-web | grep -Ei "Image:|Image ID:|Pulling|Pulled|Failed"
```

期望：

- 镜像 tag 是本次 `TAG`。
- 没有 `ImagePullBackOff`。
- 没有持续 `ErrImagePull`。

检查 API / Worker 配置：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep -E "STORAGE_|MILVUS_|ELASTICSEARCH|OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep -E "STORAGE_|MILVUS_|ELASTICSEARCH|OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
```

检查健康接口：

```bash
kubectl -n openrag port-forward svc/api 8000:8000
curl -sS http://127.0.0.1:8000/health
```

```bash
kubectl -n openrag port-forward svc/web 8080:80
curl -I http://127.0.0.1:8080
curl -sS http://127.0.0.1:8080/api/health
```

检查数据库：

```bash
kubectl -n openrag exec statefulset/postgres -- pg_isready -U openrag -d openrag
kubectl -n openrag exec statefulset/postgres -- psql -U openrag -d openrag -c "\dt"
```

检查 MinIO 路径：

```bash
mcli ls new-minio/rag-kb/openrag
mcli ls new-minio/rag-kb/milvus
```

最终验收标准：

```text
1. 不依赖 K8s 内部 milvus-minio。
2. API / Worker 的 STORAGE_* 指向 http://172.16.31.63:9000、rag-kb、openrag。
3. Milvus 的 bucketName/rootPath 是 rag-kb/milvus。
4. OPENAI_BASE_URL 指向内网模型网关。
5. EMBEDDING_MODEL 是 Qwen3-Embedding-4B。
6. EMBEDDING_DIMENSION 是 2560。
7. L1_NAV_MODEL 是 DeepSeek-V4-Flash。
8. 所有业务镜像 tag 等于本次 TAG。
9. 所有 Pod Running / Ready。
10. 所有 PVC Bound。
11. API /health 正常。
12. Web 可访问。
13. Web 反向代理 /api/health 正常。
14. 上传、解析、入库、检索链路成功。
```

## 26. 业务链路验收

在浏览器中访问 Web 后执行：

```text
1. 打开 OpenRag Web。
2. 登录。
3. 查看已有 workspace 和文件。
4. 上传一个小文件，例如 PDF 或 TXT。
5. 观察 Worker 日志，确认任务被消费。
6. 在 MinIO 的 rag-kb/openrag/<workspace>/ 下确认新增对象。
7. 执行一次检索或问答。
8. 确认 Milvus、Elasticsearch、Postgres 没有持续异常日志。
```

Worker 日志：

```bash
kubectl -n openrag logs deploy/openrag-task-worker -f
```

API 日志：

```bash
kubectl -n openrag logs deploy/openrag-api -f
```

MinIO 对象检查：

```bash
mcli ls --recursive new-minio/rag-kb/openrag | tail -50
```

验收标准：

- 上传文件后 Worker 有处理日志。
- MinIO 中出现新增业务对象。
- 检索或问答能返回结果。
- 如果登录返回 `401`，说明网络链路已经到达 API，应检查用户表、激活状态和密码，不要优先怀疑 K8s 网络。

## 27. 常见问题排查

### 27.1 ImagePullBackOff

排查：

```bash
kubectl -n openrag describe pod <POD_NAME>
```

重点看：

```text
Image
ImagePullPolicy
Events
```

处理：

- 确认 Pod 所在节点已经导入对应镜像。
- 确认业务镜像 tag 等于本次 `TAG`。
- 确认 `imagePullPolicy` 是 `IfNotPresent`。
- 如果是第三方镜像，确认该第三方镜像也已导入该节点。

### 27.2 PVC 一直 Pending

排查：

```bash
kubectl -n openrag get pvc
kubectl -n openrag describe pvc <PVC_NAME>
kubectl get storageclass
```

处理：

- 确认集群存在默认 StorageClass。
- 如果没有默认 StorageClass，需要给 PVC 增加 `storageClassName`，或让集群管理员配置默认 StorageClass。

### 27.3 API / Worker 连接不上 MinIO

排查：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep STORAGE
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep STORAGE
kubectl -n openrag run minio-check --rm -it --restart=Never \
  --image=busybox:1.36 \
  --image-pull-policy=IfNotPresent \
  -- sh -c 'nc -z 172.16.31.63 9000 && echo minio-ok'
```

处理：

- 如果 `STORAGE_BUCKET` 不是 `rag-kb`，重新检查渲染清单。
- 如果 `STORAGE_PREFIX` 不是 `openrag`，重新检查 `deploy/envs/internal.yaml`。
- 如果 `nc` 不通，检查 K8s 节点到 MinIO 的网络、防火墙和路由。
- 如果网络通但访问失败，检查 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`。

### 27.4 Milvus 仍访问旧 MinIO

排查：

```bash
kubectl -n openrag exec deploy/milvus -- env | grep MINIO
kubectl -n openrag get cm milvus-config -o yaml | grep -E "address|bucketName|rootPath"
kubectl -n openrag logs deploy/milvus --tail=200 | grep -Ei "minio|bucket|root"
```

处理：

- `MINIO_ADDRESS` 必须是 `172.16.31.63:9000`。
- `bucketName` 必须是 `rag-kb`。
- `rootPath` 必须是 `milvus`。
- 不要部署 `06-milvus-minio.yaml`。

### 27.5 模型调用失败

排查：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep -E "OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep -E "OPENAI_BASE_URL|EMBEDDING_MODEL|EMBEDDING_DIMENSION|L1_NAV_MODEL"
kubectl -n openrag get secret openrag-secrets -o yaml | grep OPENAI_API_KEY
```

处理：

- `OPENAI_BASE_URL` 必须是内网可达地址，通常带 `/v1`。
- `OPENAI_API_KEY` 必须符合内网模型网关要求。
- `Qwen3-Embedding-4B` 的 `EMBEDDING_DIMENSION` 是 `2560`。
- 如果模型网关使用自签证书或特殊路径，需要按网关要求调整配置。

### 27.6 Web 能打开但 API 不通

排查：

```bash
kubectl -n openrag port-forward svc/web 8080:80
curl -sS http://127.0.0.1:8080/app-config.js
curl -sS http://127.0.0.1:8080/api/health
kubectl -n openrag logs deploy/openrag-web --tail=100
```

处理：

- `/app-config.js` 必须返回运行时配置。
- `/api/health` 必须通过 Web Nginx 代理到 API。
- 如果 `/api/health` 不通，检查 `02-configmap-nginx.yaml` 和 `svc/api`。

## 28. 可选：使用内网 Harbor

如果你的内网有 Harbor，可以把主流程中的“每个节点导入镜像”替换为“推送 Harbor，然后让 K8s 从 Harbor 拉取”。

示例：

```bash
export TAG=1.0.9
export HARBOR=harbor.internal.example
export HARBOR_PROJECT=openrag

docker load -i openrag-k8s-images-$TAG.tar
docker login "$HARBOR"

docker tag postgres:16-alpine $HARBOR/$HARBOR_PROJECT/postgres:16-alpine
docker tag quay.io/coreos/etcd:v3.5.5 $HARBOR/$HARBOR_PROJECT/etcd:v3.5.5
docker tag milvusdb/milvus:v2.4.17 $HARBOR/$HARBOR_PROJECT/milvus:v2.4.17
docker tag docker.elastic.co/elasticsearch/elasticsearch:8.12.2 $HARBOR/$HARBOR_PROJECT/elasticsearch:8.12.2
docker tag busybox:1.36 $HARBOR/$HARBOR_PROJECT/busybox:1.36
docker tag openrag/api:$TAG $HARBOR/$HARBOR_PROJECT/api:$TAG
docker tag openrag/task-worker:$TAG $HARBOR/$HARBOR_PROJECT/task-worker:$TAG
docker tag openrag/web:$TAG $HARBOR/$HARBOR_PROJECT/web:$TAG

docker push $HARBOR/$HARBOR_PROJECT/postgres:16-alpine
docker push $HARBOR/$HARBOR_PROJECT/etcd:v3.5.5
docker push $HARBOR/$HARBOR_PROJECT/milvus:v2.4.17
docker push $HARBOR/$HARBOR_PROJECT/elasticsearch:8.12.2
docker push $HARBOR/$HARBOR_PROJECT/busybox:1.36
docker push $HARBOR/$HARBOR_PROJECT/api:$TAG
docker push $HARBOR/$HARBOR_PROJECT/task-worker:$TAG
docker push $HARBOR/$HARBOR_PROJECT/web:$TAG
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

注意：

- 当前 `scripts/render-k8s-profile.ps1` 默认保留本地镜像名，例如 `openrag/api:<TAG>`。
- 如果要使用 Harbor，需要额外维护镜像地址替换逻辑，或在部署前修改渲染后的 YAML。
- 没有 Harbor 时，不要执行本节。

## 29. 禁止操作清单

正式内网部署时不要执行：

```bash
kubectl apply -f k8s/06-milvus-minio.yaml
kubectl apply -f k8s-rendered/internal/06-milvus-minio.yaml
kubectl apply -k k8s/
```

不要把真实密钥提交到 Git：

```bash
git status --short
```

如果看到 `k8s-rendered/internal/01-secret.yaml` 或任何真实 Secret 文件出现在 Git 状态里，停止提交，先排查 `.gitignore` 和文件路径。

## 30. 部署完成判定

只有同时满足以下条件，才能认为内网 K8s 部署完成：

```text
1. 每个 K8s 节点都有本次部署所需镜像。
2. k8s-rendered/internal 使用本次 TAG。
3. openrag-secrets 已填真实值并成功 apply。
4. 不部署内部 milvus-minio。
5. Postgres Ready。
6. Milvus etcd Ready。
7. Milvus Ready 且使用外部 MinIO。
8. Elasticsearch Ready。
9. API Ready。
10. Worker Ready。
11. Web Ready。
12. API /health 正常。
13. Web /api/health 正常。
14. API / Worker 环境变量指向内网 MinIO 和内网模型网关。
15. 上传、解析、入库、检索完整链路成功。
```
