# OpenRag Kubernetes 清单

与 `docker/docker-compose.prod.yml` 对齐的一组**最小可运行** YAML（单副本、开发/POC 级资源）。生产环境请按内规调整 `resources`、`StorageClass`、高可用、备份与密钥管理。

## 前置要求

- Kubernetes ≥ 1.24  
- 节点满足 Elasticsearch：`vm.max_map_count ≥ 262144`（见 [ES 文档](https://www.elastic.co/guide/en/elasticsearch/reference/current/docker.html#_set_vm_max_map_count_to_at_least_262144)）  
- 已构建并推送镜像（与清单中 `image` 一致）：
  - `openrag/api:<tag>`、`openrag/task-worker:<tag>`、`openrag/web:<tag>`（由 `docker/Dockerfile.*` 构建）  
  - 第三方镜像见各 `Deployment`/`StatefulSet` 的 `image` 字段，可改为内网 Harbor 路径

## 快速部署

```bash
# 1. 修改密钥（必须）：从示例复制为本地文件（已在 .gitignore 中忽略，避免误提交）
cp 01-secret.example.yaml 01-secret.yaml
# 编辑 01-secret.yaml 中 stringData

# 2. 按需修改镜像地址：编辑各文件 image，或使用 Kustomize（见下）

# 3. 一次性应用（按顺序前缀已编号）
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-secret.yaml
kubectl apply -f 03-postgres.yaml
kubectl apply -f 05-milvus-etcd.yaml
kubectl apply -f 06-milvus-minio.yaml
kubectl apply -f 07-milvus.yaml
kubectl apply -f 08-elasticsearch.yaml
kubectl apply -f 13-configmap-openrag-llm.yaml
kubectl apply -f 14-configmap-openrag-web-runtime.yaml
# 等待 PostgreSQL / ES / Milvus Ready 后再起业务
kubectl -n openrag rollout status statefulset/postgres --timeout=600s
kubectl -n openrag rollout status statefulset/elasticsearch --timeout=600s
kubectl -n openrag rollout status deployment/milvus --timeout=600s

kubectl apply -f 09-api.yaml
kubectl apply -f 10-task-worker.yaml
kubectl apply -f 11-web.yaml
kubectl apply -f 12-ingress.yaml   # 可选；无 Ingress 控制器可跳过，改用 port-forward

kubectl -n openrag get pods,svc
```

## 构建并推送业务镜像（联网机构建机）

在**仓库根目录**执行（版本号与 `09-api.yaml` / `10-task-worker.yaml` / `11-web.yaml` 中 `image` 一致）：

```bash
export TAG=1.0.0
docker build -f docker/Dockerfile.api -t openrag/api:${TAG} .
docker build -f docker/Dockerfile.worker -t openrag/task-worker:${TAG} .
docker build -f docker/Dockerfile.web -t openrag/web:${TAG} .
docker push openrag/api:${TAG}
docker push openrag/task-worker:${TAG}
docker push openrag/web:${TAG}
```

## Kustomize 覆盖镜像（可选）

编辑 `overlays/private-registry/kustomization.yaml` 中 `YOUR_REGISTRY` 与 `newTag` 后：

```bash
kubectl apply -f 01-secret.yaml
# 部分 kubectl 版本默认禁止引用上级目录，需加：
kubectl apply -k overlays/private-registry/ --load-restrictor=LoadRestrictionsNone
```

根目录 `kustomization.yaml` 可直接 `kubectl apply -k `，**不包含** Secret（需先手动 `kubectl apply -f 01-secret.yaml`）。

## 数据库说明

本清单 **未** 挂载仓库内旧版 `init.sql`（可能与当前 ORM 模型不一致）。首次部署后请使用你们既定的 **迁移/建表** 流程（若后续仓库提供 Alembic，可用 Job 执行 `alembic upgrade head`）。

## 访问

- 有 Ingress：`https://<ingress-host>/`（按 `12-ingress.yaml` 修改 `host`）  
- 无 Ingress：`kubectl -n openrag port-forward svc/web 8080:80`，浏览器访问 `http://127.0.0.1:8080`；API 经 Web 同源 `/api/` 转发。

## 前端 API 运行时配置（免重打包）

`14-configmap-openrag-web-runtime.yaml` 会挂载 `app-config.js` 到 Web 容器静态目录，前端优先读取：

- `window.__OPENRAG_CONFIG__.apiBaseUrl`

默认值是 `"/api"`（由 Nginx 反代到 `http://api:8000`）。后续若只想切换 API 域名/路径：

```bash
kubectl apply -f 14-configmap-openrag-web-runtime.yaml
kubectl -n openrag rollout restart deployment/openrag-web
```

无需重打 `openrag/web` 镜像。

## 单文件上传上限

- `09-api.yaml` 中的 `MAX_UPLOAD_SIZE` 是文件内容的权威上限，当前为 `104857600` 字节（100 MiB）。如需改为 50 MiB，设置为 `52428800`。
- `12-ingress.yaml` 的 `proxy-body-size: "110m"` 是包含 multipart 开销的请求体上限，需始终略大于 API 文件上限；50 MiB 和 100 MiB 两档均可保持 `110m`。
- Web 前端和批量导入脚本会从 `GET /config/client` 读取当前 API 上限，调整 50/100 MiB 时无需再修改前端常量。

修改后重新应用 API 清单并滚动更新：

```bash
kubectl apply -f 09-api.yaml
kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
```

## 卸载（会删 PVC 数据，慎用）

```bash
kubectl delete -f 12-ingress.yaml --ignore-not-found
kubectl delete -f 11-web.yaml
kubectl delete -f 10-task-worker.yaml
kubectl delete -f 09-api.yaml
kubectl delete -f 14-configmap-openrag-web-runtime.yaml
kubectl delete -f 13-configmap-openrag-llm.yaml
kubectl delete -f 08-elasticsearch.yaml
kubectl delete -f 07-milvus.yaml
kubectl delete -f 06-milvus-minio.yaml
kubectl delete -f 05-milvus-etcd.yaml
kubectl delete -f 03-postgres.yaml
kubectl delete -f 01-secret.yaml --ignore-not-found
kubectl delete -f 00-namespace.yaml
```

若需保留数据，先备份 PVC 或删除 YAML 中 `persistentVolumeClaimRetentionPolicy` / 手动 `kubectl delete pvc ...`。
