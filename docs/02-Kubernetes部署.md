# OpenRag 在 Kubernetes 上的部署说明

> 与 [01-项目说明.md](./01-项目说明.md)、[03-使用说明.md](./03-使用说明.md)、[04-外部系统接入与API.md](./04-外部系统接入与API.md) 为同一套四份说明文档。

仓库提供 **`k8s/`** 目录下的**基线清单**（与 `docker-compose.prod.yml` 对齐，无 Helm）；生产仍需按内规调 `StorageClass`、资源、高可用与密钥方案。本文说明与 Compose 的对应关系、离线流程及检查项；具体文件名以 `k8s/README.md` 为准。

## 1. 组件对照

| Compose 服务 | K8s 建议 | 说明 |
|--------------|----------|------|
| `postgres` | `StatefulSet` + `PersistentVolumeClaim` | 生产优先托管 RDS / Cloud SQL（PostgreSQL）；自管需备份与 `postgresql.conf` |
| `milvus-etcd` | `Deployment` + `PVC` | 基线清单为单副本；生产可改为 `StatefulSet` |
| `milvus-minio` | `Deployment` + `PVC` | Milvus 对象存储；勿与业务 `STORAGE_*` MinIO 混淆 |
| `milvus` | `Deployment` + `PVC` | 镜像版本需与 `pymilvus` 匹配（参见 compose 注释） |
| `elasticsearch` | `StatefulSet` + `PVC` 或托管 ES | 单节点仅适合 PoC；生产用集群与 TLS |
| `api` | `Deployment` + `Service`（ClusterIP） | 容器端口 **8000**；探针 `GET /health` |
| `web` | `Deployment` + `Service` + `Ingress` | Nginx 容器，静态资源 + `/api` 反代 |
| `task-worker` | `Deployment`（多副本可调） | 环境变量 `WORKER_API_URL` 指向集群内 `http://<api-svc>:8000` |

## 2. 网络与 Ingress

**浏览器访问路径**应与生产 Nginx 一致：

- 静态资源：`https://<domain>/`
- API：`https://<domain>/api/` → 上游 `http://openrag-api:8000/`（注意上游端口为 **8000**，不是宿主机映射的 8001）

`Ingress` 示例（TLS 略）：

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: openrag
spec:
  rules:
    - host: rag.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: openrag-web
                port:
                  number: 80
```

若使用 **API 网关** 替代 Nginx 内嵌反代，可为网关配置 `/api` 前缀剥离规则，使后端收到的路径仍为 FastAPI 原始路由（无前缀）。

## 3. 配置与密钥

建议使用 `Secret` 存放：`POSTGRES_PASSWORD`、`SECRET_KEY`、`MINIO_ROOT_*`、`OPENAI_API_KEY` 等；使用 `ConfigMap` 存放非敏感开关（如 `ELASTICSEARCH__ENABLED`）。

与 Compose 对齐的环境变量名可参考：

- `docker/docker-compose.prod.yml` 中 `api`、`task-worker` 的 `environment` 列表
- `docker/.env.example`

**CORS**：当前 FastAPI 代码中开发白名单为固定 localhost 列表；生产若需严格按域名放行，应在应用层读取 `CORS_ORIGINS` 并合并到 `CORSMiddleware`（部署前请核对 `OpenRag/src/openrag/api/main.py` 是否与你的网关方案一致）。

## 4. 有状态服务与存储

- **PostgreSQL / Milvus / ES**：声明足够磁盘与 `storageClassName`；生产建议托管服务降低运维成本。
- **上传目录**：Compose 使用 `api-uploads` 卷；K8s 可为 API Pod 挂载 `PVC` 到 `UPLOAD_DIR`（与 `MAX_UPLOAD_SIZE` 一致考虑对象存储直传方案）。

## 5. 迁移与发布顺序

1. 先起有状态组件并等待健康（PostgreSQL → Milvus 依赖链 → Milvus → ES）。
2. 运行 **Alembic**：在 API Job 或 init 容器中执行 `alembic upgrade head`。
3. 启动 `api`，就绪后启动 `task-worker`（Worker 依赖 Broker/API 与向量/ES 可用性）。
4. 最后启动 `web` / 配置 `Ingress`。

## 6. 离线 K8s 集群部署流程

适用于 **集群节点不能访问公网镜像仓库**（或仅允许访问企业内 **私有镜像仓库 / Harbor**）的场景。思路是：**在可联网环境准备镜像与配置制品 → 搬运进内网 → 推送到内网 Registry → 部署清单中全部使用内网地址**。

### 6.1 前置条件

- 内网已部署 **容器镜像仓库**（Harbor、`registry:2`、云厂商私有 CR 等），且 **各 K8s 节点可拉取**该仓库（防火墙、DNS、`imagePullSecrets` 已就绪）。
- 具备一台可 **临时联网** 的构建机（或 CI），用于拉取基础镜像并 `docker save` / `skopeo copy`；该机与内网之间允许 **受控拷贝**（U 盘、堡垒机 scp、光盘等）。
- 若集群 **完全无外网**，Embedding / LLM 调用须走 **内网已部署的模型网关**（或允许出网的专用代理），并在 `Secret` 中配置 **内网可达的 `api_base`**，否则检索与解析中依赖外部 API 的步骤会失败。

### 6.2 镜像清单（须全部改为内网可拉取）

对照 `docker/docker-compose.prod.yml`，典型第三方镜像包括（版本号以你锁定的 compose / 安全扫描结果为准，部署前在联网机 `docker pull` 验证 tag）：

| 用途 | 镜像来源（示例，以 compose 为准） |
|------|-------------------------------------|
| PostgreSQL | `postgres:16-alpine` |
| Milvus | `milvusdb/milvus:v2.4.17`（或与 `pymilvus` 匹配的版本） |
| Milvus etcd | `quay.io/coreos/etcd:v3.5.5` |
| Milvus MinIO | `minio/minio:RELEASE.2023-03-20T20-16-18Z` |
| Elasticsearch | `docker.elastic.co/elasticsearch/elasticsearch:8.12.2` |
| 业务 | 自构建 `openrag-api`、`openrag-web`、`openrag-task-worker`（由 `docker/Dockerfile.*` 构建） |

**业务镜像**：在联网构建机执行 `docker build -f docker/Dockerfile.api ...` 等，与线上一致；**不要**依赖构建时访问公网 `pip` / `npm` 若无保障——应使用 **内网 PyPI/npm 镜像** 或 **多阶段构建且依赖层已缓存**。

以下命令默认 **Linux / macOS + Bash**；仓库根目录指含 `docker/`、`OpenRag/` 的 OpenRag 工程根。请将示例中的 **`HARBOR`、`TAG`、`跳板机IP`、`内网kubectl主机`** 换成你的环境。

### 6.3 联网跳板机：下载（docker pull）

与 `docker/docker-compose.prod.yml` 中镜像版本对齐的一次性拉取示例：

```bash
# 第三方依赖镜像
docker pull postgres:16-alpine
docker pull quay.io/coreos/etcd:v3.5.5
docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z
docker pull milvusdb/milvus:v2.4.17
docker pull docker.elastic.co/elasticsearch/elasticsearch:8.12.2

# 校验本地已存在（可选）
docker images | egrep 'postgres|etcd|minio|milvus|elasticsearch'
```

### 6.4 联网跳板机：构建业务镜像并打标签

在**仓库根目录**执行（`TAG` 自行递增，如 `1.0.0`）：

```bash
export TAG=1.0.0

# API（上下文为仓库根，与 compose 一致）
docker build -f docker/Dockerfile.api -t openrag/api:${TAG} .

# Task Worker
docker build -f docker/Dockerfile.worker -t openrag/task-worker:${TAG} .

# Web（内含前端 build，需已能 npm install；离线构建见上一节「内网 PyPI」说明）
docker build -f docker/Dockerfile.web -t openrag/web:${TAG} .
```

### 6.5 联网跳板机：打包（docker save）与压缩、校验

**单文件大包**（适合 U 盘一次拷贝；文件较大）：

```bash
export TAG=1.0.0
export OFFLINE_TAR=openrag-offline-${TAG}-images.tar

docker save -o "${OFFLINE_TAR}" \
  postgres:16-alpine \
  quay.io/coreos/etcd:v3.5.5 \
  minio/minio:RELEASE.2023-03-20T20-16-18Z \
  milvusdb/milvus:v2.4.17 \
  docker.elastic.co/elasticsearch/elasticsearch:8.12.2 \
  openrag/api:${TAG} \
  openrag/task-worker:${TAG} \
  openrag/web:${TAG}

# 压缩（可选，显著减小体积）
gzip -f "${OFFLINE_TAR}"

# 生成校验和，便于内网验收（可选）
sha256sum "${OFFLINE_TAR}.gz" > "${OFFLINE_TAR}.gz.sha256"
cat "${OFFLINE_TAR}.gz.sha256"
```

**分包**（单文件超过介质限制时，按镜像拆开 `docker save` 多次；内网对每包分别 `docker load` 即可）：

```bash
export TAG=1.0.0
export OUT=./openrag-offline-${TAG}-split
mkdir -p "${OUT}"

# 基础中间件（体积小，可一包）
docker save -o "${OUT}/01-postgres.tar" postgres:16-alpine

# Milvus 依赖
docker save -o "${OUT}/02-milvus-etcd-minio.tar" \
  quay.io/coreos/etcd:v3.5.5 \
  minio/minio:RELEASE.2023-03-20T20-16-18Z

# Milvus 本体（单独一包，便于失败重传）
docker save -o "${OUT}/03-milvus.tar" milvusdb/milvus:v2.4.17

# Elasticsearch（单独一包）
docker save -o "${OUT}/04-elasticsearch.tar" docker.elastic.co/elasticsearch/elasticsearch:8.12.2

# 业务三镜像（可再拆成三个 tar）
docker save -o "${OUT}/05-openrag-api.tar" openrag/api:${TAG}
docker save -o "${OUT}/06-openrag-task-worker.tar" openrag/task-worker:${TAG}
docker save -o "${OUT}/07-openrag-web.tar" openrag/web:${TAG}

# 逐包压缩与校验（可选）
for f in "${OUT}"/*.tar; do gzip -f "$f"; done
cat "${OUT}/SHA256SUMS"
```

内网加载示例：

```bash
export OUT=./openrag-offline-1.0.0-split
sha256sum -c "${OUT}/SHA256SUMS"   # 若已生成校验文件
for f in "${OUT}"/*.tar.gz; do gunzip -c "$f" | docker load; done
```

### 6.6 搬运至内网（下载到内网制品机）

任选其一（示例）：

```bash
# 从跳板机推到内网制品服务器（替换用户与 IP）
scp openrag-offline-${TAG}-images.tar.gz openrag-offline-${TAG}-images.tar.gz.sha256 \
  user@10.0.0.50:/data/openrag-artifacts/

# 或使用 rsync 断点续传
rsync -avP openrag-offline-${TAG}-images.tar.gz user@10.0.0.50:/data/openrag-artifacts/
```

内网制品机收到文件后建议先校验再解压：

```bash
cd /data/openrag-artifacts
sha256sum -c openrag-offline-${TAG}-images.tar.gz.sha256
```

### 6.7 内网制品机：导入（docker load）、改 tag、推送 Harbor

在能 **docker login** 到内网 Harbor 的机器上执行：

```bash
export TAG=1.0.0
export HARBOR=harbor.internal.example   # 无协议、无路径
export HARBOR_PROJECT=openrag            # Harbor 项目名，需已创建

# 1) 解压并导入（若 6.5 步未压缩则去掉 gunzip 管道）
gunzip -c openrag-offline-${TAG}-images.tar.gz | docker load

# 2) 登录 Harbor（交互输入密码；CI 可用 --password-stdin）
docker login "${HARBOR}"

# 3) 统一 retag 并 push（第三方镜像建议放在 library 或单独项目，按内规修改前缀）
docker tag postgres:16-alpine ${HARBOR}/${HARBOR_PROJECT}/postgres:16-alpine
docker push ${HARBOR}/${HARBOR_PROJECT}/postgres:16-alpine

docker tag quay.io/coreos/etcd:v3.5.5 ${HARBOR}/${HARBOR_PROJECT}/etcd:v3.5.5
docker push ${HARBOR}/${HARBOR_PROJECT}/etcd:v3.5.5

docker tag minio/minio:RELEASE.2023-03-20T20-16-18Z ${HARBOR}/${HARBOR_PROJECT}/minio:RELEASE.2023-03-20T20-16-18Z
docker push ${HARBOR}/${HARBOR_PROJECT}/minio:RELEASE.2023-03-20T20-16-18Z

docker tag milvusdb/milvus:v2.4.17 ${HARBOR}/${HARBOR_PROJECT}/milvus:v2.4.17
docker push ${HARBOR}/${HARBOR_PROJECT}/milvus:v2.4.17

docker tag docker.elastic.co/elasticsearch/elasticsearch:8.12.2 ${HARBOR}/${HARBOR_PROJECT}/elasticsearch:8.12.2
docker push ${HARBOR}/${HARBOR_PROJECT}/elasticsearch:8.12.2

docker tag openrag/api:${TAG} ${HARBOR}/${HARBOR_PROJECT}/api:${TAG}
docker push ${HARBOR}/${HARBOR_PROJECT}/api:${TAG}

docker tag openrag/task-worker:${TAG} ${HARBOR}/${HARBOR_PROJECT}/task-worker:${TAG}
docker push ${HARBOR}/${HARBOR_PROJECT}/task-worker:${TAG}

docker tag openrag/web:${TAG} ${HARBOR}/${HARBOR_PROJECT}/web:${TAG}
docker push ${HARBOR}/${HARBOR_PROJECT}/web:${TAG}
```

推送完成后，所有 K8s YAML 里的 `image:` 应改为上述 **`${HARBOR}/${HARBOR_PROJECT}/...:${TAG}`** 形式。

### 6.8 可选：containerd 节点直接导入 tar

若内网节点 **无 Docker**、仅用 containerd，可在**每个节点**或制品机执行（路径与 tar 名按实际修改）：

```bash
sudo ctr -n k8s.io images import openrag-offline-${TAG}-images.tar
# 导入后仍建议在 Harbor 中维护单一真源，由 kubelet 拉取，便于版本管理
```

### 6.9 可选：skopeo 直连同步（无 docker load 场景）

在已安装 [skopeo](https://github.com/containers/skopeo) 的联网机上，可将单个镜像同步为 tar 再迁入（示例）：

```bash
skopeo copy docker://docker.io/library/postgres:16-alpine docker-archive:postgres-16-alpine.tar:docker.io/library/postgres:16-alpine
```

内网再用 `skopeo copy docker-archive:... docker://${HARBOR}/...` 推到 Harbor（具体 `docker-archive` 语法以本机 skopeo 版本帮助为准）。

### 6.10 内网：kubectl 启动与验证（示例命令）

仓库未内置完整 Helm Chart 时，你仍可使用自维护的 `k8s/*.yaml`。以下为**通用 kubectl 流程**（文件名请按实际清单替换）：

```bash
export NS=openrag
export TAG=1.0.0
export HARBOR=harbor.internal.example
export HARBOR_PROJECT=openrag

# 命名空间
kubectl create namespace "${NS}" --dry-run=client -o yaml | kubectl apply -f -

# 若 Harbor 需拉取密钥（用户名/密码替换为真实值）
kubectl -n "${NS}" create secret docker-registry harbor-regcred \
  --docker-server="${HARBOR}" \
  --docker-username='robot$openrag+pull' \
  --docker-password='YOUR_PASSWORD_OR_TOKEN' \
  --dry-run=client -o yaml | kubectl apply -f -

# 应用配置与有状态服务（顺序：infra → 中间件 → 业务；YAML 需自备）
kubectl -n "${NS}" apply -f k8s/00-config-secret.yaml
kubectl -n "${NS}" apply -f k8s/03-postgres.yaml
kubectl -n "${NS}" apply -f k8s/20-milvus-etcd.yaml
kubectl -n "${NS}" apply -f k8s/21-milvus-minio.yaml
kubectl -n "${NS}" apply -f k8s/22-milvus.yaml
kubectl -n "${NS}" apply -f k8s/30-elasticsearch.yaml

# 等待关键依赖 Ready 后再起 API
kubectl -n "${NS}" rollout status statefulset/postgres --timeout=600s
kubectl -n "${NS}" rollout status statefulset/elasticsearch --timeout=600s

# 数据库迁移（一次性 Job；镜像与 API 相同）
kubectl -n "${NS}" delete job openrag-alembic --ignore-not-found
kubectl -n "${NS}" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: openrag-alembic
spec:
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: harbor-regcred
      containers:
        - name: alembic
          image: ${HARBOR}/${HARBOR_PROJECT}/api:${TAG}
          command: ["alembic", "upgrade", "head"]
          envFrom:
            - secretRef: { name: openrag-api-secret }
EOF

kubectl -n "${NS}" wait --for=condition=complete job/openrag-alembic --timeout=600s
kubectl -n "${NS}" logs job/openrag-alembic

# API / Worker / Web
kubectl -n "${NS}" apply -f k8s/40-api.yaml
kubectl -n "${NS}" apply -f k8s/41-task-worker.yaml
kubectl -n "${NS}" apply -f k8s/50-web.yaml
kubectl -n "${NS}" apply -f k8s/60-ingress.yaml

kubectl -n "${NS}" rollout status deployment/openrag-api --timeout=300s
kubectl -n "${NS}" get pods,svc,ingress
```

> **说明**：当前仓库 `docker/Dockerfile.api` 生产镜像**未必**包含 `alembic.ini` 与迁移脚本目录时，上述 Alembic Job 可能失败。可任选其一：**(1)** 扩展镜像将 `OpenRag/alembic`（及 `alembic.ini`）拷入 `/app`；**(2)** 在能访问该 PostgreSQL 的 CI/堡垒机执行 `alembic upgrade head`；**(3)** 使用带源码的调试镜像跑迁移。`openrag-api-secret` 名称需与你在 `k8s/00-config-secret.yaml` 中定义的一致。

**验证集群能否拉取业务镜像**（调试用）：

```bash
kubectl -n "${NS}" run pull-test --rm -it --restart=Never \
  --image=${HARBOR}/${HARBOR_PROJECT}/api:${TAG} \
  --image-pull-policy=Always \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"harbor-regcred"}]}}' \
  --command -- sh -c 'echo ok && sleep 5'
```

**API 健康检查**（Ingress 或 port-forward 后）：

```bash
kubectl -n "${NS}" port-forward svc/openrag-api 8000:8000
curl -sS http://127.0.0.1:8000/health
```

### 6.11 内网 Registry 与清单约定（摘要）

1. 所有 `Deployment` / `StatefulSet` 的 `image:` 使用 **内网 Harbor 完整路径**。  
2. 需要认证时配置 `imagePullSecrets`（见 6.10）。  
3. `imagePullPolicy` 建议 `IfNotPresent` 或与内规一致。  
4. 避免在清单中残留公网 `image:`（除非经批准的镜像代理）。

### 6.12 配置与依赖（离线特有）

- **ConfigMap / Secret**：与 `k8s/*.yaml` 一并 `kubectl apply`；敏感项不入 Git。  
- **Elasticsearch**：注意节点 `vm.max_map_count` 与 JVM 堆；见 ES 官方文档。  
- **Milvus**：etcd / MinIO 与 Milvus 的 Service 名、端口与 compose 拓扑对齐。  
- **Embedding / LLM**：配置 **内网可达** 的 HTTP 端点，避免 Pod 访问公网失败。

### 6.13 部署顺序与排障（与第 5 节一致）

1. 先确认各节点或 Harbor **可拉取**业务镜像（6.10 `pull-test`）。  
2. 按 **第 5 节** 顺序：有状态与中间件 → Alembic Job → `api` → `task-worker` → `web` / `Ingress`。  
3. `kubectl describe pod`、`kubectl logs` 排查 **ImagePullBackOff**、DNS、ES 启动超时等。

### 6.14 离线检查清单（在 第 8 节 基础上增加）

- [ ] 所有工作负载镜像均为 **内网 Registry** 地址，且节点可拉取  
- [ ] 已配置 `imagePullSecrets`（若需要）  
- [ ] 无公网 URL 残留在 Deployment、Ingress、环境变量（API 外呼除外且已走内网代理）  
- [ ] Embedding / 解析链路所依赖的 **模型 HTTP 端点** 在内网可达  
- [ ] 大镜像节点磁盘空间充足；必要时对 ES / Milvus 单独规划存储类与容量

## 7. 水平扩展建议

- **API**：`Deployment` 多副本 + Service；会话无状态，JWT 无服务端会话。
- **task-worker**：多 `Deployment` 副本或多套 Worker，确保不重复消费同一任务（当前实现以 DB/Broker 协调为准；扩缩前阅读 `broker` 与任务状态机相关代码）。
- **Milvus / ES**：按官方运维指南扩集群，而非简单加 API 副本。

## 8. 与 Compose 行为对齐的检查清单

- [ ] 集群内 `WORKER_API_URL=http://<api-service>:8000`
- [ ] Nginx 或 Ingress 将 `/api/` 代理到 **8000**
- [ ] 前端构建时若使用相对路径 `/api`，与网关规则一致
- [ ] `ELASTICSEARCH__HOSTS` 指向可达的 ES 服务（通常为 `https://` + 证书或集群内 `http://`）
- [ ] 密钥未进入镜像层，仅通过 `Secret` 注入

## 9. 参考文件

- **`k8s/`**：命名空间、Secret 示例、PostgreSQL/Milvus 依赖/ES、API、Worker、Web、Ingress 等 YAML；部署步骤见 **`k8s/README.md`**。离线打包命令中的路径可与该目录一一对应。
- `docker/docker-compose.prod.yml`：环境变量与依赖顺序的「真值来源」
- `docker/nginx/nginx.conf`：路径与上游端口
- `docker/Dockerfile.api`、`docker/Dockerfile.worker`、`docker/Dockerfile.web`：镜像构建入口

完成 K8s 清单后，建议在预发环境跑通：注册 → 建工作区 → 上传 → 任务成功 → 检索 → 服务令牌 `/service/v1/search` 冒烟测试。
