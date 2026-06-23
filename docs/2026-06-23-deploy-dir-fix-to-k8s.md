# 把目录修复（方案二）部署到内网 K8s —— 仅更新 api 镜像

> 配合 [Kubernetes 部署说明（更新版）](02-Kubernetes部署-update.md) 使用。本文只讲**本次代码改动的最小化增量部署**：构建并滚动更新 **api 一个镜像**，其余镜像全部复用。

> **范围说明（重要）**：本文修的是**后端目录问题**（你最初报的"目录树不显示"），只需 api。
> 但合并后的 `main` **同时**包含了 PR #11 的**前端文件夹拖拽上传**（改了 `web/`）。
> - 只想修目录树 bug（你的诉求）→ **api-only**，照本文做即可，web 复用旧镜像。
> - 想要完整最新 main（含拖拽上传 UI）→ 还要**额外重建 web 镜像**，见文末「可选：同时上 web」。
> 内网当前是 1.1.3，两者都还没有；二选一取决于你是否需要那个前端 UI。

## 为什么只动 api（最大化复用）

本次改动只有 `openrag/src/openrag/services/file_ingest.py` 一个文件（上传时建目录行）。因此：

| 镜像 | 是否需要重建 | 原因 |
|---|---|---|
| `openrag/api` | **是** | 上传入口 `ingest_new_file` 在 api 进程里运行，改动在此 |
| `openrag/task-worker` | 否（复用） | worker 不调用 `ingest_new_file`，行为不变；可继续用旧 tag |
| `openrag/web` | 否（复用）* | **本次目录修复**无前端改动。*注：latest main 另含 PR #11 前端拖拽上传，想要它则 web 也要重建（见文末） |
| 第三方镜像（postgres / milvus / etcd / minio / es / busybox） | 否（复用） | 完全未变，已在 Harbor 与节点上 |

**因此整套增量 = 构建 1 个镜像 + 推 1 个镜像 + 改 1 个 tag + 滚动 1 个 Deployment。**

不需要的步骤：
- **不跑 alembic 迁移**：本次无表结构变更（只是上传时多写 `is_directory` 行，表早已存在）。
- **不再回填**：内网存量目录已用 `backfill_directory_rows.py` 回填过；本次只让**新上传**自动建目录。

> 关键：k8s 的 `imagePullPolicy: IfNotPresent`，同 tag 不会重新拉取。**必须用一个新 tag**（例：当前 `1.1.3` → 新 `1.1.4`），节点才会拉到新 api 镜像。

---

## 第 0 步：内网确认当前 tag 与 Harbor 地址

```bash
# 当前 api 镜像引用（含 Harbor 地址与 tag）
kubectl -n openrag get deploy openrag-api -o jsonpath='{.spec.template.spec.containers[0].image}'; echo
#   形如 harbor.internal.example/openrag/api:1.1.3  → 记下 Harbor/项目，新 tag 取 1.1.4
```
记下：`HARBOR=harbor.internal.example`、`HARBOR_PROJECT=openrag`、`NEWTAG=1.1.4`。

## 第 1 步：构建机（联网）从最新 main 只构建 api 镜像

```powershell
cd E:\project\OpenRag
git checkout main
git pull                      # 确保是合并后的最新 main
$env:NEWTAG = "1.1.4"
$env:BUILD_PROXY = "http://http.docker.internal:3128"   # 不需要代理则 ""

docker build --build-arg BUILD_PROXY=$env:BUILD_PROXY -f docker/Dockerfile.api -t "openrag/api:$($env:NEWTAG)" .
docker image inspect "openrag/api:$($env:NEWTAG)" | Out-Null

# 只导出 api 一个镜像（体积小，传输快）
docker save -o "openrag-api-$($env:NEWTAG).tar" "openrag/api:$($env:NEWTAG)"
(Get-FileHash "openrag-api-$($env:NEWTAG).tar" -Algorithm SHA256).Hash.ToLower() + "  openrag-api-$($env:NEWTAG).tar" |
  Set-Content "openrag-api-$($env:NEWTAG).tar.sha256" -Encoding ascii
```
> 依赖层（`pip install -r requirements`）会命中 Docker 构建缓存，只有 `COPY src` 之后的层重建，很快。

## 第 2 步：搬到内网制品机并导入

用离线方式把 `openrag-api-1.1.4.tar`(+`.sha256`) 传到内网制品机，然后：

```bash
sha256sum -c openrag-api-1.1.4.tar.sha256
docker load -i openrag-api-1.1.4.tar
```

## 第 3 步：只推 api 到 Harbor

```bash
HARBOR=harbor.internal.example
HARBOR_PROJECT=openrag
NEWTAG=1.1.4

docker login "$HARBOR"
docker tag "openrag/api:$NEWTAG" "$HARBOR/$HARBOR_PROJECT/api:$NEWTAG"
docker push "$HARBOR/$HARBOR_PROJECT/api:$NEWTAG"
```
> 第三方镜像、worker、web **都不碰**——它们在 Harbor / 节点上已存在。

## 第 4 步：只把 overlay 里 api 的 tag 改成新 tag

编辑 `k8s/overlays/private-registry/kustomization.yaml`，**只改 `openrag/api` 这一项**的 `newTag`，worker / web / 第三方保持原值不动：

```yaml
  - name: openrag/api
    newName: harbor.internal.example/openrag/api
    newTag: "1.1.4"        # ← 只改这里
  # openrag/task-worker、openrag/web、各第三方镜像保持现有 tag 不变
```

渲染检查：

```bash
kubectl kustomize k8s/overlays/private-registry --load-restrictor=LoadRestrictionsNone | grep "image:"
#   确认 api 是 :1.1.4，其余镜像 tag 与现网一致、且都是 Harbor 地址
```

## 第 5 步：应用并只滚动 api

```bash
kubectl kustomize k8s/overlays/private-registry --load-restrictor=LoadRestrictionsNone | kubectl apply -f -
#   因为只有 api 的镜像 tag 变了，apply 只会更新 openrag-api 这一个 Deployment
kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
```

> 等价的更直接做法（只动 api、不依赖 overlay 一致性）：
> ```bash
> kubectl -n openrag set image deployment/openrag-api api=$HARBOR/$HARBOR_PROJECT/api:$NEWTAG
> kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
> ```
> 若用这个快捷方式，仍建议把 overlay 的 api newTag 同步改掉，避免下次 `apply -k` 把它回退。

## 第 6 步：验证

```bash
API_POD=$(kubectl -n openrag get pod -l app=openrag-api -o jsonpath='{.items[0].metadata.name}')
# 1) 新 Pod 跑的是新镜像 + 新代码
kubectl -n openrag get deploy openrag-api -o jsonpath='{.spec.template.spec.containers[0].image}'; echo
kubectl -n openrag exec "$API_POD" -c api -- python -c "import inspect,openrag.services.file_ingest as m; print('ensure_directory_path' in inspect.getsource(m.ingest_new_file))"
# 2) 健康
kubectl -n openrag exec "$API_POD" -c api -- sh -c 'curl -fsS http://127.0.0.1:8000/health || true'
```
预期：image 为 `:1.1.4`，第 1 条输出 `True`，health 返回 healthy。
最终功能验证：在页面新建空间、上传一个文件夹 → 左侧目录树**自动**出现目录层级（不再需要回填）。

## 回滚

```bash
kubectl -n openrag rollout undo deployment/openrag-api
#   或显式回到旧 tag：
#   kubectl -n openrag set image deployment/openrag-api api=$HARBOR/$HARBOR_PROJECT/api:1.1.3
```

## 可选：同时上 web（要 PR #11 的前端拖拽上传才需要）

latest main 的 `web/` 因 PR #11 **确实变了**（新增"拖文件夹自动上传"）。**只修目录树 bug 不需要它**；若你也想要这个 UI：

```powershell
# 构建机：用同一个 NEWTAG 额外构建 web
docker build --build-arg NPM_PROXY=$env:BUILD_PROXY --build-arg NPM_REGISTRY=$env:NPM_REGISTRY -f docker/Dockerfile.web -t "openrag/web:$($env:NEWTAG)" .
docker save -o "openrag-web-$($env:NEWTAG).tar" "openrag/web:$($env:NEWTAG)"
```
传输 / `docker load` / 推 Harbor（`web:$NEWTAG`）同 api；然后在 overlay 里把 `openrag/web` 的 `newTag` 也改成 `$NEWTAG`，`apply -k` 后多滚动一个：
```bash
kubectl -n openrag rollout status deployment/openrag-web --timeout=600s
```

`task-worker` 仍**无需**重建（本次无 worker 相关改动）；若团队强制三套业务镜像 tag 一致，再按 [部署文档](02-Kubernetes部署-update.md) §A/§B 一并处理。
