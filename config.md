# OpenRag 内外网 MinIO 配置生成机制

## 目标

本机制用于解决外网测试环境和内网部署环境 MinIO 配置不同导致的重复手工修改问题。

约定如下：

- `k8s/` 保持为外网环境可直接测试的基础清单，继续指向外网旧 MinIO。
- 内网新 MinIO 使用单 bucket 结构，非敏感参数放入仓库中的 profile。
- 每次外网代码、镜像、流程测试通过后，运行一个命令生成内网部署清单。
- 生成目录不包含真实 MinIO 密钥。
- 脚本负责同步待部署镜像 tag，但不处理镜像仓库地址，因为内网没有镜像仓库，也无法访问互联网。

## 推荐目录结构

```text
deploy/
  envs/
    internal.yaml              # 内网 MinIO 非敏感 profile，提交入库
scripts/
  render-k8s-profile.ps1       # 一键渲染脚本
k8s/
  ...                          # 外网旧 MinIO 基础清单
k8s-rendered/
  internal/                    # 生成的内网部署清单，建议加入 .gitignore
```

## 内网 Profile 示例

`deploy/envs/internal.yaml` 只保存非敏感配置，不保存真实 access key 或 secret key。

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

真实密钥仍由内网部署时的 `openrag-secrets` 提供。仓库中的 Secret 示例文件只保留占位符。

## 一键生成命令

推荐命令形态：

```powershell
.\scripts\render-k8s-profile.ps1 -Profile internal -Tag 1.1.1
```

如果没有传入 `-Tag`，脚本应交互提示：

```text
请输入待部署版本号，例如 1.1.1:
```

脚本生成：

```text
k8s-rendered/internal/
```

生成目录用于传入内网并执行部署。不要手工维护 `k8s-rendered/internal/`，需要变更时重新运行脚本生成。

## 镜像版本处理

内网没有镜像仓库，因此清单中的镜像名保持本地镜像名，只同步 tag：

```text
openrag/api:<Tag>
openrag/task-worker:<Tag>
openrag/web:<Tag>
```

脚本必须保持：

```yaml
imagePullPolicy: IfNotPresent
```

这表示内网节点会优先使用 `docker load` 后已经存在的本地镜像。

镜像离线流程独立于 MinIO 渲染流程：

```text
外网构建镜像 -> docker save 导出 tar -> 内网 docker load -> 应用生成后的 YAML
```

脚本不应把镜像改成 Harbor 地址，也不应尝试拉取镜像。

## 渲染规则

脚本从 `k8s/` 复制清单到 `k8s-rendered/internal/`，只替换 MinIO 相关字段和业务镜像 tag。

需要修改的文件：

```text
k8s-rendered/internal/01-secret.example.yaml
k8s-rendered/internal/07-milvus.yaml
k8s-rendered/internal/07-milvus-config.yaml
k8s-rendered/internal/09-api.yaml
k8s-rendered/internal/10-task-worker.yaml
k8s-rendered/internal/11-web.yaml
k8s-rendered/internal/15-configmap-openrag-service-conf.yaml
```

API 和 Worker 替换：

```text
STORAGE_ENDPOINT   -> minio.endpoint
STORAGE_BUCKET     -> minio.storageBucket
STORAGE_PREFIX     -> minio.storagePrefix
STORAGE_PUBLIC_URL -> minio.publicUrl
```

Milvus Deployment 替换：

```text
MINIO_ADDRESS -> minio.address:minio.port
```

Milvus ConfigMap 替换：

```text
minio.address    -> minio.address
minio.port       -> minio.port
minio.bucketName -> minio.milvusBucket
minio.rootPath   -> minio.milvusRootPath
```

`secretAccessKey` 继续保留占位符，不能写入真实密钥。

RAGFlow 兼容 `service_conf.yaml` 替换：

```text
minio.host        -> minio.address:minio.port
minio.bucket      -> minio.storageBucket
minio.prefix_path -> minio.storagePrefix
```

`password` 继续保留占位符，不能写入真实密钥。

Secret 示例文件规则：

```text
MINIO_ROOT_USER       -> 占位符或非敏感默认值
MINIO_ROOT_PASSWORD   -> CHANGE_ME...
MILVUS_SECRET_KEY     -> CHANGE_ME...
```

如需在内网部署真实 Secret，应在内网复制生成目录中的 `01-secret.example.yaml` 为 `01-secret.yaml` 后手动填入，或使用内网已有的 Secret 管理流程注入。

## 单 Bucket 兼容方式

旧模型中，OpenRag 通常按 workspace slug 作为逻辑 bucket。

旧对象路径示例：

```text
law/regulations/a.doc
test/example.pdf
```

内网新 MinIO 使用一个物理 bucket：

```text
rag-kb
```

OpenRag 业务对象通过 `STORAGE_BUCKET` 和 `STORAGE_PREFIX` 做物理路径映射：

```text
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
```

映射后：

```text
workspace=law,  object=regulations/a.doc -> rag-kb/openrag/law/regulations/a.doc
workspace=test, object=example.pdf       -> rag-kb/openrag/test/example.pdf
```

也就是说，业务代码仍可以把 `law`、`test` 当作逻辑 bucket 或 workspace slug 使用。真正访问 MinIO 时，由存储层映射到单个物理 bucket 下的不同 prefix。

Milvus 不走 OpenRag workspace 逻辑，单独使用：

```text
bucketName=rag-kb
rootPath=milvus
```

最终内网 MinIO 结构为：

```text
rag-kb/
  openrag/
    law/
    test/
  milvus/
```

## 是否需要修改代码

渲染机制本身不需要改业务代码，但内网运行的镜像必须包含单 bucket 兼容代码。

代码侧需要支持：

```text
STORAGE_BUCKET
STORAGE_PREFIX
STORAGE_PUBLIC_URL
```

并在 MinIO 访问前完成逻辑 bucket 到物理 bucket 的转换：

```text
logical bucket + object key -> STORAGE_BUCKET / STORAGE_PREFIX / logical bucket / object key
```

当前工作区已经有这类兼容思路：

```text
openrag/src/openrag/config.py
openrag/src/openrag/storage/minio_storage.py
openrag/tests/test_storage_config_parity.py
openrag/tests/test_minio_storage_single_bucket.py
```

因此部署前必须确保外网构建的新镜像包含这些改动。只替换 YAML 但继续运行旧镜像时，旧代码可能无法理解 `STORAGE_PREFIX=openrag`，会导致对象路径错误或 `NoSuchKey`。

## 脚本校验要求

脚本生成完成后应自动检查：

```text
1. k8s-rendered/internal/ 目录存在。
2. 关键 YAML 文件全部生成。
3. API 和 Worker 的 STORAGE_* 等于 profile 中的值。
4. Milvus 的 MINIO_ADDRESS、bucketName、rootPath 等于 profile 中的值。
5. service_conf.yaml 中的 minio.host、bucket、prefix_path 等于 profile 中的值。
6. 生成目录中不允许出现外网旧 MinIO endpoint。
7. 生成目录中不允许出现真实 MinIO secret。
8. openrag/api、openrag/task-worker、openrag/web 的 tag 等于输入版本号。
9. imagePullPolicy 必须保持 IfNotPresent。
10. 如果本机有 kubectl，则执行 kubectl kustomize k8s-rendered/internal。
```

如果本机没有 `kubectl`，第 10 项可以跳过并给出提示；前 1 到 9 项必须通过。

## 外网执行流程

1. 在外网环境保留 `k8s/` 指向旧 MinIO，完成开发和测试。
2. 确认单 bucket 兼容测试通过。
3. 构建新版本镜像：

```powershell
$env:TAG = "1.1.1"
docker build -f docker/Dockerfile.api -t openrag/api:$env:TAG .
docker build -f docker/Dockerfile.worker -t openrag/task-worker:$env:TAG .
docker build -f docker/Dockerfile.web -t openrag/web:$env:TAG .
```

4. 导出离线镜像包：

```powershell
docker save -o openrag-offline-$env:TAG-images.tar `
  openrag/api:$env:TAG `
  openrag/task-worker:$env:TAG `
  openrag/web:$env:TAG
```

5. 生成内网部署清单：

```powershell
.\scripts\render-k8s-profile.ps1 -Profile internal -Tag $env:TAG
```

6. 将以下内容传入内网：

```text
openrag-offline-<Tag>-images.tar
k8s-rendered/internal/
```

## 内网执行流程

1. 导入镜像：

```bash
docker load -i openrag-offline-<Tag>-images.tar
docker images | grep openrag
```

2. 准备真实 Secret：

```bash
cp k8s-rendered/internal/01-secret.example.yaml k8s-rendered/internal/01-secret.yaml
vi k8s-rendered/internal/01-secret.yaml
```

必须填入：

```text
POSTGRES_PASSWORD
SECRET_KEY
MINIO_ROOT_USER
MINIO_ROOT_PASSWORD
MILVUS_SECRET_KEY
OPENAI_API_KEY 或内网模型网关需要的占位值
```

3. 应用 Secret：

```bash
kubectl apply -f k8s-rendered/internal/01-secret.yaml
```

4. 应用清单：

```bash
kubectl apply -k k8s-rendered/internal
```

5. 检查工作负载：

```bash
kubectl -n openrag get pods,svc,ingress,pvc
kubectl -n openrag rollout status deployment/milvus --timeout=600s
kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
kubectl -n openrag rollout status deployment/openrag-task-worker --timeout=600s
kubectl -n openrag rollout status deployment/openrag-web --timeout=600s
```

## 内网验收检查

检查 API 和 Worker 环境变量：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep STORAGE
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep STORAGE
```

期望：

```text
STORAGE_ENDPOINT=http://172.16.31.63:9000
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
STORAGE_PUBLIC_URL=http://172.16.31.63:9000
```

检查 Milvus：

```bash
kubectl -n openrag exec deploy/milvus -- env | grep MINIO
kubectl -n openrag logs deploy/milvus | grep -Ei 'minio|bucket|root'
```

期望：

```text
MINIO_ADDRESS=172.16.31.63:9000
bucketName=rag-kb
rootPath=milvus
```

检查对象路径：

```bash
mcli ls new-minio/rag-kb/openrag
mcli ls new-minio/rag-kb/milvus
```

上传或处理新文件后，应在以下路径看到新增业务对象：

```text
new-minio/rag-kb/openrag/<workspace>/
```

如果出现 `NoSuchKey`，优先检查失败日志中的 object key 是否存在：

```bash
mcli stat new-minio/rag-kb/<object_name>
```

## 失败处理原则

- 如果生成清单仍出现外网旧 MinIO endpoint，停止部署，重新检查 profile 和渲染脚本。
- 如果内网 Pod 使用旧镜像 tag，重新运行脚本并确认 `-Tag` 参数。
- 如果镜像 tag 正确但代码仍按多 bucket 访问，说明内网导入的镜像没有包含单 bucket 兼容代码，需要重新构建和导入镜像。
- 如果 `NoSuchKey` 指向 `rag-kb/openrag/...` 下缺对象，优先判断是对象迁移不完整，而不是 YAML 渲染错误。
- 如果 Milvus 对象不在 `rag-kb/milvus/` 下，检查 `07-milvus-config.yaml` 是否被正确渲染，以及 Milvus 是否确实加载了该配置文件。

## 不在本机制内处理的内容

- 不创建或维护内网镜像仓库。
- 不自动上传或导入镜像 tar。
- 不提交真实 Secret。
- 不自动迁移旧 MinIO 对象到新 MinIO。
- 不修改业务代码逻辑，只要求待部署镜像已经包含单 bucket 兼容能力。
- 不调整 Ingress host、资源配额、StorageClass 等非 MinIO 参数。

