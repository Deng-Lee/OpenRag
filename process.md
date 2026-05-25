# OpenRag 外部 MinIO 单桶迁移流程

本文档描述将 OpenRag 当前本地 MinIO 能力迁移到外部 S3-compatible MinIO 的方案 A：保留旧数据，迁移业务对象，并通过 Milvus Backup/Restore 迁移向量库数据。

## 1. 目标

外部对象存储只提供一个 bucket：

```text
Endpoint: http://172.16.31.63:9000
Access Key: raguser
Secret Key: <REDACTED>
Bucket: rag-kb
Versioning: 未启用
```

迁移后的对象布局：

```text
rag-kb/
  milvus/
    ...Milvus 新实例数据...

  milvus-backups/<timestamp>/
    ...Milvus Backup 中转数据...

  openrag/
    <workspace_slug>/
      ...原始文件对象...
      hierarchy/
        ...L0/L1/L2 hierarchy 对象...
```

注意：不要把真实 Secret Key 写入仓库、脚本或文档。生产执行时从 Kubernetes Secret、CI/CD Secret 或运维密钥系统注入。

## 2. 总体策略

采用蓝绿迁移：

1. 旧环境保持可回滚，只在迁移窗口冻结写入。
2. 新环境连接外部 MinIO 的 `rag-kb` bucket。
3. OpenRag 业务对象从旧的 workspace bucket 迁移到 `rag-kb/openrag/<workspace_slug>/`。
4. Milvus 数据使用 `milvus-backup` 从旧 Milvus 备份，再恢复到新 Milvus。
5. 新 Milvus 使用 `bucketName=rag-kb` 和 `rootPath=milvus`。
6. 验证通过后切 API/worker 流量。

不要直接在旧 Milvus 上修改 `minio.rootPath`。Milvus 已运行实例改变 rootPath 可能导致旧数据不可读。

## 3. 前置准备

### 3.0 远端执行环境约定

本迁移面向远端部署环境执行。若 OpenRag 部署在四台机器上，先判断它们是 Kubernetes 集群节点还是四台独立 Docker/进程机器。

本仓库当前部署文件以 Kubernetes 为主，以下命令默认从一台具备 `kubectl` 权限的运维机、跳板机或任意 master/control-plane 节点执行。不要分别登录四台机器手工查数据库；只要四台机器连接的是同一个 Postgres，workspace、files、chunks 这类数据库基线只需要对共享 Postgres 查询一次。

需要逐台或逐 Pod 确认的内容：

- API/worker/Milvus 是否都已加载新环境变量或 ConfigMap。
- Pod 是否分布在不同节点，以及是否全部滚动更新成功。
- 每个 API/worker Pod 是否能访问外部 MinIO 和新 Milvus。

只需要确认一次的内容：

- `workspace.slug` 清单。
- `files`、`document_chunks`、`tasks` 等数据库统计。
- 旧 MinIO 对象布局和对象数量。
- Milvus collection/entity count，前提是所有服务连接同一个 Milvus。

先在远端执行环境中设置命名空间变量：

```bash
export NS=openrag
```

确认当前四台机器/节点和 Pod 分布：

```bash
kubectl get nodes -o wide
kubectl -n "$NS" get pods -o wide
kubectl -n "$NS" get svc
```

确认关键工作负载名称：

```bash
kubectl -n "$NS" get statefulset
kubectl -n "$NS" get deployment
```

本仓库 K8s YAML 中 Postgres 是 `StatefulSet/postgres`，API 是 `Deployment/openrag-api`，worker 是 `Deployment/openrag-task-worker`，Milvus 是 `Deployment/milvus`。

### 3.1 工具准备

执行节点需要具备：

```bash
kubectl
psql / pg_dump
mc
milvus-backup
python 或可执行 PyMilvus 脚本的环境
```

`mc` alias 示例：

```bash
mc alias set old-minio http://<old-minio-endpoint> <old-access-key> <old-secret-key>
mc alias set new-minio http://172.16.31.63:9000 raguser '<REDACTED>'
```

### 3.2 确认旧 Milvus 对象配置

当前仓库的 Milvus manifest 只显式配置了：

```text
MINIO_ADDRESS
MINIO_ACCESS_KEY_ID
MINIO_SECRET_ACCESS_KEY
```

未显式配置 `bucketName/rootPath`。Milvus Docker Compose 默认通常是：

```text
bucketName = a-bucket
rootPath = files
```

正式执行前必须通过旧 Milvus 配置、日志或旧 MinIO 对象路径确认真实值。

### 3.3 识别两套 OpenRag 存储层

当前代码库里存在两套互相独立的存储层，迁移时必须分别处理：

| 存储层 | 文件 | 配置来源 | 单桶模式现状 |
| --- | --- | --- | --- |
| RAGFlowMinio（旧 RAGFlow 链路） | `openrag/rag/utils/minio_conn.py` | `openrag/common/settings.py` 读取 `conf/service_conf.yaml` 中的 `minio` 配置 | 已支持 `bucket + prefix_path`，调用方传入的 bucket 会变成 key 前缀 |
| MinioStorage（新 OpenRag 链路） | `openrag/src/openrag/storage/minio_storage.py` | `openrag/src/openrag/config.py` 的 `StorageConfig`，即 `STORAGE_` 环境变量 | 当前不支持单桶模式，方法参数 `bucket_name` 会被直接当作物理 bucket |

结论：

- `RAGFlowMinio` 不需要为单桶模式改代码，但必须确认运行时存在 `conf/service_conf.yaml`，并且其中 `minio.bucket=rag-kb`、`minio.prefix_path=openrag` 已正确配置。
- `MinioStorage` 必须改造；否则 API、worker、预览、删除、移动、hierarchy 写入仍会尝试访问 `<workspace_slug>` 这个物理 bucket。
- 只设置 `STORAGE_*` 环境变量无法影响 `RAGFlowMinio`；只设置 `service_conf.yaml` 也无法影响 `MinioStorage`。

### 3.4 版本和配置兼容性

当前仓库存在多份部署配置：

| 文件 | Milvus 版本 | 当前 MinIO 配置状态 |
| --- | --- | --- |
| `docker/docker-compose.dev.yml` | `milvusdb/milvus:v2.4.17` | 只配置 `MINIO_ADDRESS`、AK/SK |
| `docker/docker-compose.prod.yml` | `milvusdb/milvus:v2.4.17` | 只配置 `MINIO_ADDRESS`、AK/SK |
| `k8s/07-milvus.yaml` | `milvusdb/milvus:v2.4.17` | 只配置 `MINIO_ADDRESS`、AK/SK |
| `openrag/docker/docker-compose.yml` | `milvusdb/milvus:v2.3.3` | 只配置 `MINIO_ADDRESS`，且没有 AK/SK |

目标迁移环境应统一到 Milvus `v2.4.17`。`openrag/docker/docker-compose.yml` 要么标记为旧 compose，不参与本次迁移；要么同步升级到 `v2.4.17` 并补齐外部 MinIO 配置。

`milvus-backup` 工具版本必须与源 Milvus 和目标 Milvus 兼容。正式执行前先在测试环境完成一次小 collection 备份恢复演练。

## 4. 阶段一：盘点基线

目的：拿到迁移前的数量基线，后续每一步都按基线验收。

### 4.1 盘点 workspace 和文件

执行位置：

- Kubernetes：在具备 `kubectl` 权限的远端运维机/跳板机上执行，通过 Postgres Pod 进入数据库。
- Docker Compose：登录运行 Postgres 容器的服务器执行。
- 四台机器共享同一个 Postgres 时，只需要查询一次；不需要每台机器都查。

#### 4.1.1 Kubernetes 环境查询 workspace_slug

先找到 Postgres Pod：

```bash
export NS=openrag
export POSTGRES_POD="$(kubectl -n "$NS" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')"
echo "$POSTGRES_POD"
```

确认数据库可连接：

```bash
kubectl -n "$NS" exec "$POSTGRES_POD" -- pg_isready -U openrag -d openrag
```

查询 workspace slug：

```bash
kubectl -n "$NS" exec -it "$POSTGRES_POD" -- \
  psql -U openrag -d openrag -c "select id, slug, name from workspaces order by id;"
```

如果需要导出到文件，直接在本机保存查询结果：

```bash
kubectl -n "$NS" exec "$POSTGRES_POD" -- \
  psql -U openrag -d openrag -A -F ',' -c "select id, slug, name from workspaces order by id;" \
  > inventory-workspaces.csv
```

#### 4.1.2 Docker Compose 环境查询 workspace_slug

登录运行 Postgres 容器的服务器：

```bash
ssh <user>@<postgres-server>
```

进入 compose 目录后执行：

```bash
docker compose ps
docker compose exec postgres psql -U openrag -d openrag -c "select id, slug, name from workspaces order by id;"
```

如果容器名不是 `postgres`，先查询容器名：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep postgres
docker exec -it <postgres-container-name> psql -U openrag -d openrag -c "select id, slug, name from workspaces order by id;"
```

#### 4.1.3 数据库统计 SQL

```sql
select id, slug, name from workspaces order by id;

select
  w.slug,
  count(f.id) as file_rows,
  sum(case when f.is_directory then 1 else 0 end) as directory_rows,
  sum(case when not f.is_directory then 1 else 0 end) as document_rows
from workspaces w
left join files f on f.workspace_id = w.id
group by w.slug
order by w.slug;

select count(*) as chunk_rows from document_chunks;
```

Kubernetes 下直接执行完整统计：

```bash
kubectl -n "$NS" exec -it "$POSTGRES_POD" -- psql -U openrag -d openrag <<'SQL'
select id, slug, name from workspaces order by id;

select
  w.id,
  w.slug,
  w.name,
  count(f.id) as file_rows,
  sum(case when f.is_directory then 1 else 0 end) as directory_rows,
  sum(case when not f.is_directory then 1 else 0 end) as document_rows,
  sum(case when f.processing_status = 'completed' then 1 else 0 end) as completed_documents,
  sum(case when f.processing_status = 'failed' then 1 else 0 end) as failed_documents
from workspaces w
left join files f on f.workspace_id = w.id
group by w.id, w.slug, w.name
order by w.id;

select count(*) as chunk_rows from document_chunks;
SQL
```

验收：

- 保存 workspace slug 清单。
- 保存每个 workspace 的文件行数、目录行数、文档行数。
- 保存 `document_chunks` 总数。

#### 4.1.4 当前执行基线（2026-05-19）

本次迁移前数据库基线已在 Kubernetes 环境中完成盘点：

```text
执行节点: master1 / 172.16.22.13
Namespace: openrag
Postgres Pod: postgres-0
```

workspace 清单：

| id | slug | name |
| --- | --- | --- |
| 1 | law | 法律法规 |
| 2 | test | 测试 |

文件统计：

| workspace | name | file_rows | directory_rows | document_rows | completed_documents | failed_documents |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| law | 法律法规 | 1573 | 1 | 1572 | 1406 | 116 |
| test | 测试 | 3 | 1 | 2 | 2 | 0 |

切片统计：

| metric | count |
| --- | ---: |
| document_chunks | 44778 |

注意：`law` 工作区迁移前已有 `116` 个 failed documents。后续验收应以该基线为准，不应将这些历史失败文档误判为 MinIO 迁移导致的新失败。

### 4.2 盘点旧业务对象

执行位置：

- 在已配置 `mc` 且能访问旧 MinIO 的远端运维机/跳板机执行。
- 不要求在四台业务机器上分别执行；对象存储是共享服务，只需要从一个能访问它的环境盘点一次。
- 如果只有某一台服务器能访问旧 MinIO，就登录那台服务器执行。

先确认 `mc` alias：

```bash
mc alias list
mc alias set old-minio http://<old-minio-endpoint> <old-access-key> <old-secret-key>
```

确认旧 MinIO bucket 布局：

```bash
mc ls old-minio
```

对每个 workspace slug 执行：

```bash
mc ls --recursive old-minio/<workspace_slug> > inventory-old-<workspace_slug>.txt
wc -l inventory-old-<workspace_slug>.txt
```

如果旧环境不是 workspace 独立 bucket，而是单 bucket + prefix，则改用：

```bash
mc ls --recursive old-minio/<old-physical-bucket>/<old-prefix>/<workspace_slug> \
  > inventory-old-<workspace_slug>.txt
wc -l inventory-old-<workspace_slug>.txt
```

抽样查看对象：

```bash
head -20 inventory-old-<workspace_slug>.txt
mc stat old-minio/<workspace_slug>/<sample-object>
```

验收：

- 每个 workspace 都有对象清单。
- 对象数量和业务预期一致。
- 抽样确认原始文件、`hierarchy/*.abstract.md`、`hierarchy/*.overview.md`、`hierarchy/*/chunks/*.md` 存在。

#### 4.2.1 当前执行基线（2026-05-19）

本次旧业务对象盘点已通过 MinIO Client `mcli` 完成。由于服务器上 `mc` 命令为 Midnight Commander，本次实际命令使用 `mcli` 代替文档中的 `mc`。

访问方式：

```text
执行节点: master1 / 172.16.22.13
源 MinIO alias: old-minio
访问路径: kubectl port-forward svc/milvus-minio 19000:9000 后使用 http://127.0.0.1:19000
```

旧 MinIO bucket 布局：

| bucket | 说明 | 本次处理 |
| --- | --- | --- |
| a-bucket | Milvus 默认对象 bucket | 不作为 OpenRag 业务对象迁移源；后续通过 Milvus Backup/Restore 处理 |
| law | 当前数据库 workspace `law` 的业务对象 bucket | 纳入业务对象迁移 |
| test | 当前数据库 workspace `test` 的业务对象 bucket | 纳入业务对象迁移 |
| test-ws | 当前数据库无对应 workspace | 本次不纳入业务对象迁移，除非业务另行确认需要保留 |

业务对象数量基线：

| source path | inventory file | object_count |
| --- | --- | ---: |
| old-minio/law | inventory-old-law.txt | 48994 |
| old-minio/test | inventory-old-test.txt | 178 |

注意：MinIO 对象数不需要与数据库 `files` 行数一一对应。一个文档可能对应原始文件、L0/L1/L2 hierarchy 文件和多个 chunk markdown 对象；因此 `law` 的对象数量高于文档行数和 chunk 行数是预期现象。

### 4.3 盘点 Milvus collection

用 PyMilvus 或现有运维脚本记录：

```python
from pymilvus import connections, utility, Collection

connections.connect(host="<old-milvus-host>", port="19530")
for name in utility.list_collections():
    c = Collection(name)
    print(name, c.num_entities)
```

验收：

- 保存 collection 名称。
- 保存每个 collection 的 entity count。
- 记录索引状态和可加载状态。

#### 4.3.1 当前执行基线（2026-05-19）

本次 Milvus collection 基线已在 Kubernetes 环境中通过 `openrag-api` Pod 内的 PyMilvus 完成盘点：

```text
执行节点: master1 / 172.16.22.13
Namespace: openrag
执行位置: deploy/openrag-api
Milvus service: milvus:19530
```

执行方式：

```bash
kubectl -n openrag exec -i deploy/openrag-api -- python - <<'PY'
from pymilvus import connections, utility, Collection

connections.connect(host="milvus", port="19530")
for name in utility.list_collections():
    c = Collection(name)
    print(name, c.num_entities)
PY
```

Milvus collection/entity count 基线：

| collection | entity_count |
| --- | ---: |
| openrag_chunks | 47979 |
| openrag_layers | 3022 |

注意：Milvus `openrag_chunks` entity count 与数据库 `document_chunks` 行数不要求完全一致，可能包含历史重跑、删除未清理或不同层级向量等数据。后续 Milvus Backup/Restore 验收应以本节 collection/entity count 为准。

## 5. 阶段二：在本地仓库完成代码和配置兼容

目的：在不改动线上 Pod 内文件的前提下，让 OpenRag 支持“单物理 bucket + workspace key 前缀”，并把所有代码、Kubernetes YAML、测试和镜像发布步骤沉淀到本地仓库中。阶段二完成后，只应得到一组可构建、可测试、可回滚的新镜像和部署配置；暂不切生产流量。

执行原则：

- 不在服务器容器或 Pod 内临时修改代码。Pod 重启、滚动更新或调度到其他节点后，临时修改会丢失且不可审计。
- 所有正式改动先进入本地 Git 仓库，再通过镜像构建、镜像推送和 Kubernetes YAML 发布到环境。
- 真实 Secret Key 不写入仓库、提交记录或文档。仓库中只保留 `<REDACTED>`、示例值或 Secret 引用。
- 先在本地和测试环境验证，再进入阶段三冻结写入和正式迁移。

阶段二输出物：

- `MinioStorage` 支持单桶模式：`rag-kb/openrag/<workspace_slug>/<object_key>`。
- API 与 worker 的 Kubernetes 配置能注入 `STORAGE_BUCKET=rag-kb` 和 `STORAGE_PREFIX=openrag`。
- 旧 RAGFlow 链路能通过 `conf/service_conf.yaml` 使用 `bucket=rag-kb` 和 `prefix_path=openrag`。
- Milvus 目标配置能显式设置 `bucketName=rag-kb` 和 `rootPath=milvus`。
- 本地测试、镜像构建、Kubernetes dry-run、测试环境 rollout 验证命令都有记录。

### 5.1 子任务：确认本地仓库和部署入口

任务主题：确认本阶段所有改动都从本地仓库出发，并明确最终会影响哪些镜像和 Kubernetes 资源。

涉及修改：

- 暂不修改代码。
- 确认后续会修改或新增的文件：
  - `openrag/src/openrag/config.py`
  - `openrag/src/openrag/storage/minio_storage.py`
  - `openrag/tests/test_minio_storage_single_bucket.py`
  - `k8s/09-api.yaml`
  - `k8s/10-task-worker.yaml`
  - `k8s/01-secret.example.yaml`
  - `k8s/07-milvus.yaml` 或新增 `k8s/07-milvus-config.yaml`
  - 如需挂载旧 RAGFlow 配置，新增或调整对应 ConfigMap YAML

本地操作：

```bash
git status --short
git checkout -b feat/external-minio-single-bucket

rg -n "class StorageConfig|bucket:|public_url" openrag/src/openrag/config.py
rg -n "class MinioStorage|path_style_http_url|ensure_bucket|put_file|remove_directory|move_file" openrag/src/openrag/storage/minio_storage.py
rg -n "STORAGE_ENDPOINT|STORAGE_ACCESS_KEY|STORAGE_SECRET_KEY|MILVUS_HOST|MINIO_ADDRESS" k8s
```

验证方式：

- `git status --short` 只显示预期中的本地改动。
- 能定位 `StorageConfig`、`MinioStorage`、API/worker/Milvus YAML 的现有配置入口。
- 明确当前镜像构建入口：

```bash
ls docker/Dockerfile.api docker/Dockerfile.worker
```

### 5.2 子任务：为 StorageConfig 增加单桶前缀配置

任务主题：让新 OpenRag 存储层可以从环境变量读取 `STORAGE_PREFIX=openrag`。

涉及修改：

- 修改 `openrag/src/openrag/config.py`：
  - 在 `StorageConfig` 中新增：

```python
prefix: Optional[str] = None
```

  - 字段保持 `SettingsConfigDict(env_prefix="STORAGE_")`，使环境变量 `STORAGE_PREFIX` 自动生效。
  - 不改变 `bucket` 的含义：配置 `STORAGE_BUCKET=rag-kb` 时，它表示目标物理 bucket。

验证方式：

```bash
cd openrag
PYTHONPATH=src STORAGE_BUCKET=rag-kb STORAGE_PREFIX=openrag python - <<'PY'
from openrag.config import StorageConfig

cfg = StorageConfig()
assert cfg.bucket == "rag-kb"
assert cfg.prefix == "openrag"
print(cfg.bucket, cfg.prefix)
PY
```

预期输出：

```text
rag-kb openrag
```

### 5.3 子任务：改造 MinioStorage 的 bucket/key 解析

任务主题：调用方继续传 `workspace.slug` 和逻辑 `object_key`，存储层内部把它解析到目标单桶物理路径。

涉及修改：

- 修改 `openrag/src/openrag/storage/minio_storage.py`。
- 新增内部解析方法，建议命名为 `_resolve_location(bucket_name, object_key)`：

```text
未配置 STORAGE_BUCKET 或 STORAGE_BUCKET 为空：
  physical bucket = bucket_name
  physical key = object_key

配置 STORAGE_BUCKET=rag-kb 且 STORAGE_PREFIX=openrag：
  physical bucket = rag-kb
  physical key = openrag/<bucket_name>/<object_key>

配置 STORAGE_BUCKET=rag-kb 且 STORAGE_PREFIX 为空：
  physical bucket = rag-kb
  physical key = <bucket_name>/<object_key>
```

- 所有 MinIO 读写删改方法必须统一调用该解析方法：
  - `put_file`
  - `get_file_to_path`
  - `remove_file`
  - `remove_directory`
  - `move_file`
  - `file_exists`
  - `open_object_stream`
  - `read_object_bytes`
  - `get_object_text`
  - `put_document_hierarchy`
  - `remove_document_hierarchy`
  - `move_document_hierarchy`

- `move_file` 和 `move_document_hierarchy` 的 `CopySource` 也必须使用解析后的物理 bucket/key，不能继续只使用逻辑 bucket/key。
- `ensure_bucket(bucket_name)` 在单桶模式下只确认逻辑通过，不调用 `bucket_exists` 或 `make_bucket`；多 bucket 旧模式保留原自动建 bucket 行为。

验证方式：

- 新增 `openrag/tests/test_minio_storage_single_bucket.py`，至少覆盖以下场景：
  - `STORAGE_BUCKET=rag-kb`、`STORAGE_PREFIX=openrag` 时，`law/docs/a.pdf` 解析为 `rag-kb/openrag/law/docs/a.pdf`。
  - `STORAGE_BUCKET=rag-kb`、`STORAGE_PREFIX=openrag/` 时，不产生重复斜杠。
  - 未配置单桶时，仍使用旧模式：bucket 为 `law`，key 为 `docs/a.pdf`。
  - 单桶模式下 `ensure_bucket("law")` 不调用 `bucket_exists` / `make_bucket`。
  - `path_style_http_url("law", "docs/a.pdf")` 返回 `http://172.16.31.63:9000/rag-kb/openrag/law/docs/a.pdf`。
  - `remove_directory("law", "docs")` 对物理前缀 `openrag/law/docs/` 执行递归删除。
  - `move_file("law", "old.pdf", "new.pdf")` 从 `openrag/law/old.pdf` 复制到 `openrag/law/new.pdf`。

本地测试命令：

```bash
cd openrag
PYTHONPATH=src pytest tests/test_minio_storage_single_bucket.py -q
```

预期结果：

```text
所有新增 MinioStorage 单桶模式测试通过。
```

### 5.4 子任务：更新 API 和 worker 的 Kubernetes 存储配置

任务主题：让新镜像启动后连接外部 MinIO，并在运行时启用单桶模式。

涉及修改：

- 修改 `k8s/09-api.yaml`：
  - `STORAGE_ENDPOINT` 改为外部 MinIO endpoint：`http://172.16.31.63:9000` 或 `172.16.31.63:9000`，以代码实际解析方式为准。
  - 新增 `STORAGE_BUCKET=rag-kb`。
  - 新增 `STORAGE_PREFIX=openrag`。
  - 新增或调整 `STORAGE_PUBLIC_URL=http://172.16.31.63:9000`。
  - `STORAGE_ACCESS_KEY` / `STORAGE_SECRET_KEY` 继续从 Secret 注入，不写明文。

- 修改 `k8s/10-task-worker.yaml`：
  - 与 API 保持同一组 `STORAGE_*` 环境变量。

- 修改 `k8s/01-secret.example.yaml`：
  - 增加外部 MinIO 凭据示例 key，或者明确 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 在目标环境中应替换为外部 MinIO 的 `raguser` 和对应 Secret。
  - 真实 `STORAGE_SECRET_KEY` 只写入部署环境的实际 Secret，不提交仓库。

验证方式：

```bash
kubectl apply --dry-run=client -f k8s/09-api.yaml
kubectl apply --dry-run=client -f k8s/10-task-worker.yaml
kubectl apply --dry-run=client -f k8s/01-secret.example.yaml
```

测试环境 rollout 后验证：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep -Ei 'STORAGE_(TYPE|ENDPOINT|BUCKET|PREFIX|PUBLIC_URL)'
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep -Ei 'STORAGE_(TYPE|ENDPOINT|BUCKET|PREFIX|PUBLIC_URL)'
```

预期结果：

```text
STORAGE_TYPE=minio
STORAGE_ENDPOINT=http://172.16.31.63:9000
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
STORAGE_PUBLIC_URL=http://172.16.31.63:9000
```

### 5.5 子任务：补齐旧 RAGFlowMinio 的运行时配置

任务主题：保证旧 RAGFlow 存储链路也写入 `rag-kb/openrag/<workspace_slug>/...`，避免只改 `STORAGE_*` 后新旧两条存储链路不一致。

涉及修改：

- 根据当前镜像内 `get_project_base_directory("conf", "service_conf.yaml")` 的实际路径，选择一种方式在本地仓库沉淀配置：
  - 新增 ConfigMap YAML，例如 `k8s/08-openrag-service-conf.yaml`。
  - 或修改已有 API/worker Deployment，挂载包含 `conf/service_conf.yaml` 的 ConfigMap。
- `service_conf.yaml` 的目标内容必须包含：

```yaml
minio:
  host: 172.16.31.63:9000
  user: raguser
  password: <REDACTED>
  bucket: rag-kb
  prefix_path: openrag
  secure: false
```

- API 和 worker 都要能读取同一份配置。
- 保持：

```text
STORAGE_IMPL=MINIO
```

验证方式：

测试环境 rollout 后，在 API 和 worker Pod 内分别执行：

```bash
kubectl -n openrag exec deploy/openrag-api -- python - <<'PY'
from common.file_utils import get_project_base_directory
from common import settings

print(get_project_base_directory("conf", "service_conf.yaml"))
print(settings.MINIO.get("bucket"))
print(settings.MINIO.get("prefix_path"))
PY

kubectl -n openrag exec deploy/openrag-task-worker -- python - <<'PY'
from common.file_utils import get_project_base_directory
from common import settings

print(get_project_base_directory("conf", "service_conf.yaml"))
print(settings.MINIO.get("bucket"))
print(settings.MINIO.get("prefix_path"))
PY
```

预期结果：

```text
bucket = rag-kb
prefix_path = openrag
```

注意：`STORAGE_PREFIX=openrag` 只影响 `openrag/src/openrag/storage/minio_storage.py`；不会自动影响 `openrag/rag/utils/minio_conn.py` 读取的 `settings.MINIO.prefix_path`。

### 5.6 子任务：补齐 Milvus 外部 MinIO 配置

任务主题：让目标 Milvus 显式使用外部 MinIO 的 `rag-kb/milvus` 路径，避免继续使用默认 `a-bucket/files` 或旧集群内 MinIO。

涉及修改：

- 修改 `k8s/07-milvus.yaml`，或新增 `k8s/07-milvus-config.yaml` 后纳入部署顺序。
- 新增 `milvus-config` ConfigMap：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: milvus-config
  namespace: openrag
data:
  milvus.yaml: |
    minio:
      address: 172.16.31.63
      port: 9000
      accessKeyID: raguser
      secretAccessKey: <REDACTED>
      bucketName: rag-kb
      rootPath: milvus
      useSSL: false
```

- 在 `Deployment/milvus` 中挂载 `milvus.yaml` 到 Milvus v2.4.17 会加载的配置路径。实际路径必须先在测试环境验证，不能直接在生产上试错。
- 如果最终通过 Secret 注入 `secretAccessKey`，ConfigMap 中不要出现真实密码。

验证方式：

```bash
kubectl apply --dry-run=client -f k8s/07-milvus.yaml
```

测试环境启动后验证：

```bash
kubectl -n openrag rollout status deployment/milvus --timeout=600s
kubectl -n openrag logs deploy/milvus | grep -Ei 'minio|bucket|rootPath|rag-kb|milvus'
mcli ls new-minio/rag-kb/milvus
```

预期结果：

- Milvus Pod Ready。
- 日志或运行时配置能证明 `bucketName=rag-kb`、`rootPath=milvus` 已生效。
- 新写入向量数据落到 `rag-kb/milvus/...`。

### 5.7 子任务：本地构建镜像并发布到镜像仓库

任务主题：把本地仓库中的代码改动变成可部署镜像，避免服务器端临时改代码。

涉及修改：

- 不直接修改代码；执行镜像构建和镜像 tag。
- API 镜像使用 `docker/Dockerfile.api`。
- worker 镜像使用 `docker/Dockerfile.worker`。
- 修改 `k8s/09-api.yaml` 和 `k8s/10-task-worker.yaml` 的 image tag，指向新构建的版本。

示例命令：

```bash
export IMAGE_TAG=external-minio-$(date +%Y%m%d%H%M)
export REGISTRY=<your-registry>/<your-project>

docker build -f docker/Dockerfile.api -t "$REGISTRY/openrag-api:$IMAGE_TAG" .
docker build -f docker/Dockerfile.worker -t "$REGISTRY/openrag-task-worker:$IMAGE_TAG" .

docker push "$REGISTRY/openrag-api:$IMAGE_TAG"
docker push "$REGISTRY/openrag-task-worker:$IMAGE_TAG"
```

Kubernetes YAML 中同步 image：

```text
k8s/09-api.yaml:
  image: <your-registry>/<your-project>/openrag-api:<IMAGE_TAG>

k8s/10-task-worker.yaml:
  image: <your-registry>/<your-project>/openrag-task-worker:<IMAGE_TAG>
```

验证方式：

```bash
docker image inspect "$REGISTRY/openrag-api:$IMAGE_TAG" >/dev/null
docker image inspect "$REGISTRY/openrag-task-worker:$IMAGE_TAG" >/dev/null

kubectl apply --dry-run=client -f k8s/09-api.yaml
kubectl apply --dry-run=client -f k8s/10-task-worker.yaml
```

预期结果：

- 本地镜像存在。
- 镜像已推送到目标 registry。
- YAML dry-run 通过。

### 5.8 子任务：测试环境部署和端到端验证

任务主题：在冻结生产写入前，确认新代码和新配置确实能访问外部 MinIO，并且对象路径符合目标布局。

涉及修改：

- 只在测试环境或预发布环境 `kubectl apply`。
- 生产环境暂不切换，直到阶段三冻结写入前确认全部验证通过。

测试环境部署：

```bash
kubectl -n openrag apply -f k8s/01-secret.yaml
kubectl -n openrag apply -f k8s/09-api.yaml
kubectl -n openrag apply -f k8s/10-task-worker.yaml

kubectl -n openrag rollout status deployment/openrag-api --timeout=600s
kubectl -n openrag rollout status deployment/openrag-task-worker --timeout=600s
```

验证方式：

1. 确认配置注入：

```bash
kubectl -n openrag exec deploy/openrag-api -- env | grep -Ei 'STORAGE_(TYPE|ENDPOINT|BUCKET|PREFIX|PUBLIC_URL)'
kubectl -n openrag exec deploy/openrag-task-worker -- env | grep -Ei 'STORAGE_(TYPE|ENDPOINT|BUCKET|PREFIX|PUBLIC_URL)'
```

2. 用 API 或后台任务上传一个测试文件到测试 workspace。

3. 用 `mcli` 验证对象落点：

```bash
mcli ls --recursive new-minio/rag-kb/openrag/<workspace_slug> | head
```

4. 验证 URL 生成规则：

```sql
select object_key, object_url
from document_chunks
where object_url is not null
order by id desc
limit 10;
```

预期 URL 形态：

```text
http://172.16.31.63:9000/rag-kb/openrag/<workspace_slug>/<object_key>
```

5. 验证常用业务动作：

- 新文件上传成功。
- 新文件预览成功。
- 新文件语义检索成功。
- hierarchy 对象写入 `rag-kb/openrag/<workspace_slug>/hierarchy/...`。
- 删除或移动测试文件时，外部 MinIO 中对应对象同步变化。

阶段二通过标准：

- 本地单元测试通过。
- Kubernetes YAML dry-run 通过。
- 新 API/worker 镜像可以启动并 Ready。
- API 和 worker 均读取到 `STORAGE_BUCKET=rag-kb`、`STORAGE_PREFIX=openrag`。
- 旧 RAGFlow `settings.MINIO["bucket"] == "rag-kb"` 且 `settings.MINIO["prefix_path"] == "openrag"`。
- 新上传业务对象落到 `rag-kb/openrag/<workspace_slug>/...`。
- 新 Milvus 写入路径落到 `rag-kb/milvus/...`。
- 未发现写入旧 workspace bucket 的新对象。

## 6. 阶段三：冻结写入

目的：保证旧数据在迁移期间不再变化。

前置门禁：

- 第 5 阶段的新代码、新镜像、新 Kubernetes YAML 已在本地仓库完成，并在测试环境验证通过。
- 新 API/worker 镜像 tag、对应 YAML、外部 MinIO Secret 注入方式、Milvus 外部 MinIO 配置都已记录。
- 生产环境 API/worker 此时仍保持旧镜像和旧 MinIO 配置，不能在本阶段提前切到 `rag-kb/openrag/...`。
- 如果第 5 阶段产物未准备完成，应停止迁移，不进入冻结写入。

操作：

1. 暂停 API 上传、删除、移动、重处理入口，或将 API 置为维护模式。
2. 停止 worker：

```bash
kubectl -n "$NS" scale deployment/openrag-task-worker --replicas=0
```

3. 等待任务收敛：

```sql
select status, count(*)
from tasks
group by status
order by status;
```

验收：

- 无 running/started/pending 中的写入类任务，或业务确认这些任务可以丢弃/重跑。
- 旧 MinIO 对象数量在两次间隔检查中一致。
- 记录冻结开始时间。
- 生产 API/worker 尚未应用第 5 阶段的新镜像和新存储配置。

## 7. 阶段四：安全备份

目的：形成回滚锚点。

### 7.0 备份当前 Kubernetes 部署状态

第 5 阶段改为本地仓库产物准备后，回滚不只依赖数据备份，还依赖旧镜像和旧配置。正式迁移前必须保存当前生产资源快照。

```bash
export NS=openrag
export MIGRATION_TS="$(date +%Y%m%d%H%M%S)"
mkdir -p "migration-backup-$MIGRATION_TS"

kubectl -n "$NS" get deployment openrag-api openrag-task-worker milvus milvus-etcd milvus-minio redis -o yaml \
  > "migration-backup-$MIGRATION_TS/deployments-before-minio-migration.yaml"

kubectl -n "$NS" get statefulset postgres elasticsearch mysql -o yaml \
  > "migration-backup-$MIGRATION_TS/statefulsets-before-minio-migration.yaml"

kubectl -n "$NS" get configmap -o yaml \
  > "migration-backup-$MIGRATION_TS/configmaps-before-minio-migration.yaml"

kubectl -n "$NS" get secret -o yaml \
  > "migration-backup-$MIGRATION_TS/secrets-before-minio-migration.yaml"
chmod 600 "migration-backup-$MIGRATION_TS/secrets-before-minio-migration.yaml"

kubectl -n "$NS" get deployment openrag-api \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}' \
  > "migration-backup-$MIGRATION_TS/openrag-api-image.txt"

kubectl -n "$NS" get deployment openrag-task-worker \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}' \
  > "migration-backup-$MIGRATION_TS/openrag-task-worker-image.txt"

kubectl -n "$NS" exec deploy/openrag-api -- env | grep -Ei 'STORAGE|MINIO|MILVUS|STORAGE_IMPL' \
  > "migration-backup-$MIGRATION_TS/openrag-api-storage-env.txt"

kubectl -n "$NS" exec deploy/openrag-task-worker -- env | grep -Ei 'STORAGE|MINIO|MILVUS|STORAGE_IMPL' \
  > "migration-backup-$MIGRATION_TS/openrag-task-worker-storage-env.txt"
```

注意：`secrets-before-minio-migration.yaml` 和 `*-storage-env.txt` 可能包含敏感信息，只能保存在受控运维目录，不要提交到 Git 仓库、文档或聊天记录。

验收：

- 旧 API/worker 镜像 tag 已记录。
- 旧 Deployment/StatefulSet/ConfigMap/Secret YAML 已保存。
- 旧 API/worker 存储和 Milvus 环境变量已保存。
- 备份目录路径和保存位置已记录。

### 7.1 备份 Postgres

如果远端环境可以直连 Postgres：

```bash
pg_dump -h <postgres-host> -U <postgres-user> -d openrag -Fc -f openrag-before-minio-migration.dump
```

如果只能通过 Kubernetes Pod 访问 Postgres，先确保第 4.1 节已经设置：

```bash
export NS=openrag
export POSTGRES_POD="$(kubectl -n "$NS" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}')"
```

在 Postgres Pod 内执行 dump，并复制到当前运维机：

```bash
kubectl -n "$NS" exec "$POSTGRES_POD" -- \
  pg_dump -U openrag -d openrag -Fc -f /tmp/openrag-before-minio-migration.dump

kubectl -n "$NS" cp \
  "$POSTGRES_POD":/tmp/openrag-before-minio-migration.dump \
  ./openrag-before-minio-migration.dump

kubectl -n "$NS" exec "$POSTGRES_POD" -- \
  rm -f /tmp/openrag-before-minio-migration.dump
```

验收：

```bash
pg_restore -l openrag-before-minio-migration.dump | head
```

#### 7.1.1 当前执行结果（2026-05-19）

本次 Postgres 备份已通过 Kubernetes `postgres-0` Pod 内的 `pg_dump` 创建，并复制到 `master1` 当前目录：

```text
执行节点: master1 / 172.16.22.13
Namespace: openrag
Postgres Pod: postgres-0
备份文件: ./openrag-before-minio-migration.dump
文件大小: 11M
```

备份文件校验已通过。由于 `master1` 主机未安装 `pg_restore`，本次将 dump 文件复制回 `postgres-0` Pod 后，在 Pod 内执行 `pg_restore -l` 验证。

`pg_restore -l` 校验摘要：

```text
Archive created at: 2026-05-19 07:38:11 UTC
Database: openrag
TOC Entries: 198
Compression: gzip
Dump Version: 1.15-0
Format: CUSTOM
Dumped from database version: 16.13
Validation: pg_restore -l succeeded inside postgres-0 Pod
```

说明：`kubectl cp` 过程中出现的 `tar: removing leading '/' from member names` 是复制绝对路径时的常见提示，不表示备份失败。

### 7.2 备份旧 MinIO 和 PVC

本节包含两类备份：

- 对象级备份：使用 MinIO Client 将旧 MinIO 对象 mirror 到当前运维机备份目录。
- 卷级备份：如集群支持 `VolumeSnapshot`，为旧 MinIO PVC 创建快照；如果不支持，则由底层存储平台按 PVC/PV 信息创建快照。

本环境中 `mc` 命令为 Midnight Commander，MinIO Client 已安装为 `mcli`，以下命令使用 `mcli`。

#### 7.2.1 旧 MinIO 对象级备份

先准备备份目录：

```bash
export NS=openrag
export MIGRATION_TS="${MIGRATION_TS:-$(date +%Y%m%d%H%M%S)}"
export BACKUP_DIR="migration-backup-$MIGRATION_TS"

mkdir -p "$BACKUP_DIR/old-minio"
df -h "$BACKUP_DIR"
```

确认旧 MinIO alias 可用：

```bash
mcli alias list
mcli ls old-minio
```

本次数据库基线中有效 workspace 为 `law` 和 `test`，旧 MinIO 对应 bucket 已在阶段一确认：

```text
old-minio/law   48994 objects
old-minio/test    178 objects
```

执行对象级备份：

```bash
mcli mirror --overwrite old-minio/law  "$BACKUP_DIR/old-minio/law"
mcli mirror --overwrite old-minio/test "$BACKUP_DIR/old-minio/test"
```

生成远端和本地清单：

```bash
mcli ls --recursive old-minio/law  > "$BACKUP_DIR/inventory-old-law.remote.txt"
mcli ls --recursive old-minio/test > "$BACKUP_DIR/inventory-old-test.remote.txt"

find "$BACKUP_DIR/old-minio/law"  -type f > "$BACKUP_DIR/inventory-old-law.local.txt"
find "$BACKUP_DIR/old-minio/test" -type f > "$BACKUP_DIR/inventory-old-test.local.txt"
```

数量验收：

```bash
wc -l "$BACKUP_DIR/inventory-old-law.remote.txt"
wc -l "$BACKUP_DIR/inventory-old-law.local.txt"
wc -l "$BACKUP_DIR/inventory-old-test.remote.txt"
wc -l "$BACKUP_DIR/inventory-old-test.local.txt"
```

预期：

```text
inventory-old-law.remote.txt   = 48994
inventory-old-law.local.txt    = 48994
inventory-old-test.remote.txt  = 178
inventory-old-test.local.txt   = 178
```

抽样验收：

```bash
head -20 "$BACKUP_DIR/inventory-old-law.remote.txt"
head -20 "$BACKUP_DIR/inventory-old-test.remote.txt"

find "$BACKUP_DIR/old-minio/law" -type f | head -20
find "$BACKUP_DIR/old-minio/test" -type f | head -20
```

确认旧 MinIO 服务仍可访问：

```bash
mcli ls old-minio
mcli ls old-minio/law | head
mcli ls old-minio/test | head
```

记录备份目录和容量：

```bash
du -sh "$BACKUP_DIR"
echo "$BACKUP_DIR" | tee openrag-minio-migration-backup-dir.txt
```

#### 7.2.1.1 当前执行结果（2026-05-19）

旧 MinIO 对象级备份已完成：

```text
执行节点: master1 / 172.16.22.13
备份目录: /root/openrag-minio-migration-backup-20260519160404
备份大小: 567M
```

对象数量验收：

| workspace | object_count |
| --- | ---: |
| law | 48994 |
| test | 178 |

该备份目录已写入：

```text
/root/openrag-minio-migration-backup-latest.txt
```

#### 7.2.2 旧 MinIO PVC 快照

旧 MinIO PVC 名称：

```text
milvus-minio-data
```

先确认 PVC/PV 绑定关系：

```bash
kubectl -n "$NS" get pvc milvus-minio-data -o wide
kubectl -n "$NS" get pvc milvus-minio-data -o yaml > "$BACKUP_DIR/pvc-milvus-minio-data-before-snapshot.yaml"
kubectl get pv -o yaml > "$BACKUP_DIR/pv-before-snapshot.yaml"
```

检查集群是否支持 Kubernetes 原生快照：

```bash
kubectl api-resources | grep -i volumesnapshot
kubectl get volumesnapshotclass
```

如果能看到 `volumesnapshots.snapshot.storage.k8s.io` 和至少一个 `VolumeSnapshotClass`，设置快照类：

```bash
export SNAP_CLASS="$(kubectl get volumesnapshotclass -o jsonpath='{.items[0].metadata.name}')"
echo "$SNAP_CLASS"
```

创建旧 MinIO PVC 快照：

```bash
cat <<EOF | kubectl apply -f -
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: milvus-minio-data-snap-$MIGRATION_TS
  namespace: openrag
spec:
  volumeSnapshotClassName: $SNAP_CLASS
  source:
    persistentVolumeClaimName: milvus-minio-data
EOF
```

验收快照状态：

```bash
kubectl -n "$NS" get volumesnapshot milvus-minio-data-snap-$MIGRATION_TS
kubectl -n "$NS" describe volumesnapshot milvus-minio-data-snap-$MIGRATION_TS

kubectl -n "$NS" get volumesnapshot milvus-minio-data-snap-$MIGRATION_TS \
  -o jsonpath='{.metadata.name}{" ready="}{.status.readyToUse}{" source="}{.spec.source.persistentVolumeClaimName}{" content="}{.status.boundVolumeSnapshotContentName}{"\n"}'
```

预期：

```text
milvus-minio-data-snap-<timestamp> ready=true source=milvus-minio-data content=<VolumeSnapshotContent 名称>
```

保存快照记录：

```bash
kubectl -n "$NS" get volumesnapshot milvus-minio-data-snap-$MIGRATION_TS \
  -o yaml > "$BACKUP_DIR/volumesnapshot-milvus-minio-data-$MIGRATION_TS.yaml"

kubectl get volumesnapshotcontent \
  -o yaml > "$BACKUP_DIR/volumesnapshotcontents-$MIGRATION_TS.yaml"
```

如果集群没有 `VolumeSnapshotClass`，不要继续创建 `VolumeSnapshot`。此时有两种处理方式：

- 如果底层存储平台能独立创建快照，则将已导出的 PVC/PV YAML 交给存储平台管理员，由其按 PV 后端卷 ID 创建快照，并把快照 ID 和恢复方式写入。
- 如果底层存储平台也不创建快照，则必须明确记录“PVC 快照不可用，已用对象级备份替代，风险已接受”，并在后续阶段禁止删除、清空或重绑定旧 PVC。

存储平台快照记录模板：

```bash
cat > "$BACKUP_DIR/storage-platform-snapshots.md" <<'EOF'
# Storage Platform Snapshots

- PVC: milvus-minio-data
  PV:
  StorageClass:
  Backend volume ID:
  Snapshot ID:
  Restore method:
EOF
```

#### 7.2.2.1 当前执行结论（2026-05-19）

本环境已确认安装了 Kubernetes VolumeSnapshot 相关 CRD：

```text
volumesnapshotclasses.snapshot.storage.k8s.io/v1
volumesnapshotcontents.snapshot.storage.k8s.io/v1
volumesnapshots.snapshot.storage.k8s.io/v1
```

但当前集群没有配置任何 `VolumeSnapshotClass`：

```text
kubectl get volumesnapshotclass
No resources found
```

因此本次不创建 Kubernetes `VolumeSnapshot`。旧 MinIO PVC `milvus-minio-data` 的 Kubernetes 原生快照不可用。

当前处理结论：

- 已完成旧 MinIO 对象级备份，备份范围覆盖当前数据库有效 workspace：`law`、`test`。
- `law` 对象数量已验收为 `48994`。
- `test` 对象数量已验收为 `178`。
- 本次以对象级备份替代旧 MinIO PVC 快照。
- 已知风险：PVC 级别的整体回滚能力不可用，未纳入对象级备份范围的历史 bucket、MinIO 底层元数据或底层卷状态不能通过 Kubernetes VolumeSnapshot 恢复。
- 风险接受：本次迁移以 Postgres dump、旧 MinIO 对象级备份、后续 Milvus Backup/Restore 作为主要回滚锚点，接受不保存 `milvus-minio-data` PVC 快照的风险。

验收：

- `old-minio/law` 本地备份文件数为 `48994`。
- `old-minio/test` 本地备份文件数为 `178`。
- 备份目录可读，`du -sh "$BACKUP_DIR"` 正常返回。
- 旧 MinIO 服务仍可通过 `mcli ls old-minio` 访问。
- 本环境未创建 Kubernetes `VolumeSnapshot`。
- 已保存 PVC/PV YAML。
- 已明确记录“集群无 VolumeSnapshotClass，PVC 快照不可用，已用对象级备份替代，风险已接受”。

### 7.3 记录旧 Milvus 相关 PVC 状态

本环境已经在 7.2.2.1 明确确认：集群无 `VolumeSnapshotClass`，Kubernetes 原生 PVC 快照不可用。本次不再要求为旧 Milvus、旧 etcd、旧 MinIO 创建 PVC 快照，也不把“PVC 快照可恢复”作为后续阶段的前置条件。

本节只做三件事：

1. 导出旧 PVC/PV 绑定信息，便于回滚时确认旧 Deployment 仍指向原 PVC。
2. 明确旧 PVC 在迁移过程中不得删除、不得清空、不得重绑定。
3. 用 Postgres dump、旧 MinIO 对象级备份、Milvus Backup/Restore 替代 PVC 快照作为主要回滚锚点。

当前仓库和线上环境对应旧 PVC：

```text
milvus-data
milvus-etcd-data
milvus-minio-data
```

执行记录：

```bash
export NS=openrag
export MIGRATION_TS="${MIGRATION_TS:-$(date +%Y%m%d%H%M%S)}"
export BACKUP_DIR="migration-backup-$MIGRATION_TS"
mkdir -p "$BACKUP_DIR"

kubectl -n "$NS" get pvc milvus-data milvus-etcd-data milvus-minio-data -o wide \
  | tee "$BACKUP_DIR/pvc-milvus-related-before-stage11.txt"

kubectl -n "$NS" get pvc milvus-data milvus-etcd-data milvus-minio-data \
  -o yaml > "$BACKUP_DIR/pvc-milvus-related-before-stage11.yaml"

kubectl get pv -o yaml > "$BACKUP_DIR/pv-before-stage11.yaml"
```

再次确认快照能力和当前风险接受状态：

```bash
kubectl api-resources | grep -i volumesnapshot
kubectl get volumesnapshotclass
```

本环境预期结果：

```text
kubectl get volumesnapshotclass
No resources found
```

记录结论：

```bash
cat > "$BACKUP_DIR/pvc-snapshot-risk-accepted.md" <<EOF
# PVC Snapshot Risk Accepted

Timestamp: $MIGRATION_TS
Namespace: $NS

- Cluster has VolumeSnapshot CRDs, but no VolumeSnapshotClass.
- Kubernetes native PVC snapshots are unavailable.
- No PVC snapshot was created for milvus-data, milvus-etcd-data, or milvus-minio-data.
- Old PVCs must remain untouched during migration.
- Rollback anchors are:
  - Postgres dump
  - Old MinIO object-level backup
  - Milvus Backup/Restore files
  - Saved Kubernetes Deployment/ConfigMap/Secret YAML and image tags

Accepted risk:
PVC-level rollback is unavailable. The migration must use new PVCs for the new Milvus/etcd path and must not clear or delete old PVCs before the observation window ends.
EOF
```

验收：

- 已保存 `milvus-data`、`milvus-etcd-data`、`milvus-minio-data` 的 PVC/PV YAML。
- 已明确记录“集群无 VolumeSnapshotClass，PVC 快照不可用，已用对象级备份和 Milvus Backup 替代，风险已接受”。
- 后续第 11 阶段只能创建新 PVC 并让新 Milvus/etcd 使用新 PVC，不能清空或复用旧 etcd PVC。

## 8. 阶段五：迁移 OpenRag 业务对象

目的：把旧业务对象从旧 workspace bucket 复制到外部 MinIO 的目标单桶路径。此阶段只搬对象，不切换生产 API/worker。

关键约束：

- 生产 API/worker 仍保持旧配置，继续以旧对象路径为准；不能在本阶段应用第 5 阶段的新 API/worker YAML。
- 目标路径必须与第 5 阶段 `MinioStorage` 单桶映射一致：`rag-kb/openrag/<workspace_slug>/<object_key>`。
- 本环境中 `mc` 命令是 Midnight Commander，实际执行时应使用已安装的 MinIO Client `mcli`。以下命令如写 `mc`，本环境替换为 `mcli`。
- 本次数据库基线只包含 workspace `law` 和 `test`，`test-ws` 当前数据库无对应 workspace，除非业务确认，否则不纳入业务对象迁移。
- `a-bucket` 是旧 Milvus 默认对象 bucket，不作为 OpenRag 业务对象迁移源。

执行 `mc mirror` 前，先确认旧 MinIO 的实际对象布局。当前迁移命令默认旧环境是“每个 workspace 一个物理 bucket”：

```bash
mc ls old-minio
mc ls --recursive old-minio/<workspace_slug> | head
```

如果旧环境确实是多 bucket 布局，对每个 workspace 执行：

```bash
mc mirror \
  old-minio/<workspace_slug> \
  new-minio/rag-kb/openrag/<workspace_slug>
```

如果旧环境已经启用了单 bucket 布局，例如对象在 `<old-physical-bucket>/<old-prefix>/<workspace_slug>/...`，则源路径应改为：

```bash
mc mirror \
  old-minio/<old-physical-bucket>/<old-prefix>/<workspace_slug> \
  new-minio/rag-kb/openrag/<workspace_slug>
```

常见判断方式：

- `mc ls old-minio` 能看到多个 workspace slug bucket：使用多 bucket 源路径。
- `mc ls old-minio` 只看到一个业务 bucket，且递归对象路径以 `<prefix>/<workspace_slug>/` 开头：使用单 bucket 源路径。

因为目标 bucket 未启用 versioning，本流程只迁移当前对象版本。

本次执行范围：

```bash
mcli mirror old-minio/law  new-minio/rag-kb/openrag/law
mcli mirror old-minio/test new-minio/rag-kb/openrag/test
```

验收：

```bash
mc ls --recursive old-minio/<workspace_slug> > inventory-old-<workspace_slug>.txt
mc ls --recursive new-minio/rag-kb/openrag/<workspace_slug> > inventory-new-<workspace_slug>.txt

wc -l inventory-old-<workspace_slug>.txt
wc -l inventory-new-<workspace_slug>.txt
```

再抽样比对：

```bash
mc stat old-minio/<workspace_slug>/<sample-object>
mc stat new-minio/rag-kb/openrag/<workspace_slug>/<sample-object>
```

验收标准：

- 新旧对象数量一致。
- 抽样对象大小一致。
- 抽样原始文件可下载打开。
- 抽样 hierarchy markdown 可读取。
- 生产 API/worker 仍未切到新镜像和新 `STORAGE_*` 配置。

## 9. 阶段六：创建 Milvus 备份

目的：用 Milvus 官方备份工具导出 collection 元数据、segment 和索引数据。

执行前要求：

- 第 5 阶段准备的新 Milvus 外部 MinIO 配置此时仍不能应用到旧 Milvus。
- 旧 Milvus 在备份期间必须继续使用旧 MinIO 路径，避免备份源和运行源不一致。
- 确认旧 Milvus 实际版本、bucketName、rootPath；不要只假设 `a-bucket/files`。
- 当前旧 MinIO bucket 布局中存在 `a-bucket`，它很可能是旧 Milvus 默认对象 bucket；正式备份配置仍必须通过旧 Milvus 配置、日志或对象路径确认。
- 选择与 Milvus `v2.4.17` 兼容的 `milvus-backup` 版本。
- 如源环境仍使用 `openrag/docker/docker-compose.yml` 的 Milvus `v2.3.3`，先单独验证 backup 工具兼容性；不通过时优先将源环境升级或改用与 `v2.3.3` 兼容的 backup 工具。
- 先在测试 collection 上执行 create/list/restore 演练，再进入生产全量备份。

#### 9.0.1 当前旧 Milvus 配置确认结果（2026-05-19）

本次已通过 Milvus Deployment、Milvus 日志和旧 MinIO 对象路径确认旧 Milvus 实际配置：

```text
Milvus image/version: milvusdb/milvus:v2.4.17
Milvus service: milvus:19530
旧 MinIO endpoint: milvus-minio:9000
bucketName: a-bucket
rootPath: files
useSSL: false
```

证据一：Milvus 日志中出现以下配置：

```text
[bucketname=a-bucket] [root=files]
configuration: [address=milvus-minio:9000, bucket_name=a-bucket, root_path=files, ... useSSL=false]
```

证据二：旧 MinIO 中存在 Milvus 数据对象：

```text
old-minio/a-bucket/files/
old-minio/a-bucket/files/index_files/...
old-minio/a-bucket/files/insert_log/...
old-minio/a-bucket/files/stats_log/...
```

因此后续 `milvus-backup` 源配置必须使用：

```yaml
bucketName: a-bucket
rootPath: files
backupBucketName: a-bucket
backupRootPath: backup/<timestamp>
```

### 9.1 旧 Milvus backup 配置

示例 `backup-source.yaml`：

```yaml
milvus:
  address: <old-milvus-host>
  port: 19530
  authorizationEnabled: false
  tlsMode: 0

minio:
  storageType: minio
  address: <old-minio-host>
  port: 9000
  accessKeyID: <old-access-key>
  secretAccessKey: <old-secret-key>
  useSSL: false
  useIAM: false

  bucketName: <old-milvus-bucket>
  rootPath: <old-milvus-root-path>

  backupBucketName: <old-milvus-bucket>
  backupRootPath: backup/<timestamp>
```

如果旧 Milvus 没显式配置，先按实际确认结果填写，常见默认值是：

```text
old-milvus-bucket = a-bucket
old-milvus-root-path = files
```

### 9.2 执行备份

全量备份：

```bash
./milvus-backup --config backup-source.yaml create -n openrag-milvus-<timestamp>
```

按 collection 备份：

```bash
./milvus-backup --config backup-source.yaml create -c <collection_name> -n openrag-milvus-<timestamp>
```

验收：

```bash
./milvus-backup --config backup-source.yaml list
mc ls --recursive old-minio/<old-milvus-bucket>/backup/<timestamp>
```

本环境执行时使用 `mcli` 替代上面的 `mc`。

验收标准：

- backup 状态成功。
- 备份目录存在。
- 备份对象数量大于 0。

## 10. 阶段七：搬运 Milvus 备份到新 bucket

因为旧 Milvus 对象存储和新外部 MinIO 是不同 endpoint，需要把备份文件搬到 `rag-kb`。

关键约束：

- 这里只搬 Milvus backup 文件，不搬 `a-bucket` 下的在线 Milvus 数据目录。
- backup 文件目标路径是 `rag-kb/milvus-backups/<timestamp>`，不是 `rag-kb/milvus`。
- `rag-kb/milvus` 只能由阶段 11/12 中的新 Milvus restore 写入。
- 本环境执行时使用 `mcli` 替代示例中的 `mc`。

```bash
mc mirror \
  old-minio/<old-milvus-bucket>/backup/<timestamp> \
  new-minio/rag-kb/milvus-backups/<timestamp>
```

验收：

```bash
mc ls --recursive old-minio/<old-milvus-bucket>/backup/<timestamp> > milvus-backup-old.txt
mc ls --recursive new-minio/rag-kb/milvus-backups/<timestamp> > milvus-backup-new.txt

wc -l milvus-backup-old.txt
wc -l milvus-backup-new.txt
```

验收标准：

- 新旧备份文件数量一致。
- 抽样备份对象大小一致。

## 11. 阶段八：启动空的新 Milvus

目的：让目标 Milvus 使用新对象存储路径。

关键要求：

- 这是第 5 阶段 Milvus 外部 MinIO 配置第一次允许进入生产执行面的阶段。
- API/worker 此时仍不切换到第 5 阶段的新镜像和新存储配置；worker 应继续保持停止状态。
- 新 Milvus 连接外部 MinIO。
- `bucketName=rag-kb`。
- `rootPath=milvus`。
- 目标 etcd 应为空，不能直接复用旧 etcd 元数据再改 rootPath。
- 使用第 5.6 节准备并验证过的 Milvus YAML/ConfigMap。不要在旧 Milvus Deployment 上手工临时改环境变量或容器内配置。
- 当前集群无 `VolumeSnapshotClass`，PVC 快照不可用，因此本阶段禁止清空、删除或重绑定旧 PVC。

本阶段只允许采用“新 PVC + 新配置”的方式启动目标 Milvus：

```text
旧 etcd PVC: milvus-etcd-data
新 etcd PVC: milvus-etcd-data-external-minio

旧 Milvus 本地 PVC: milvus-data
新 Milvus 本地 PVC: milvus-data-external-minio
```

旧 PVC 必须原样保留，用于阶段 14 之前的回滚。新 PVC 用于启动空的目标 Milvus/etcd，避免把旧 etcd 元数据和旧 Milvus rootPath 带入新对象存储。

### 11.1 设置执行变量

如果本次迁移时间戳已经确定为 `20260519174905`，直接固定使用该值：

```bash
export NS=openrag
export MIGRATION_TS=20260519174905
export BACKUP_DIR="migration-backup-$MIGRATION_TS"
export NEW_ETCD_PVC=milvus-etcd-data-external-minio
export NEW_MILVUS_PVC=milvus-data-external-minio
mkdir -p "$BACKUP_DIR/stage11-before"
```

### 11.2 确认前置条件

本步骤只确认，不做修改：

```bash
kubectl -n "$NS" get deploy openrag-task-worker -o jsonpath='{.spec.replicas}{"\n"}'
ls -lh ./openrag-before-minio-migration.dump
mcli ls new-minio/rag-kb/milvus-backups/$MIGRATION_TS | head
mcli ls --recursive new-minio/rag-kb/milvus-backups/$MIGRATION_TS | head
```

预期：

- `openrag-task-worker` 副本数为 `0`。
- Postgres dump 文件存在且非空。
- `rag-kb/milvus-backups/20260519174905` 已存在 Milvus backup 文件。
- 阶段 10 的新旧 Milvus backup 文件数量已经核对一致。

如果以上任一项不满足，不要进入第 11 阶段。

### 11.3 保存切换前的 Kubernetes 状态

保存旧 Milvus/etcd Deployment 和 PVC 引用。注意这里是保存状态，不是创建 PVC 快照。

```bash
kubectl -n "$NS" get deploy milvus milvus-etcd -o yaml \
  > "$BACKUP_DIR/stage11-before/deploy-milvus-etcd-before-stage11.yaml"

kubectl -n "$NS" get svc milvus milvus-etcd -o yaml \
  > "$BACKUP_DIR/stage11-before/svc-milvus-etcd-before-stage11.yaml"

kubectl -n "$NS" get pvc milvus-data milvus-etcd-data milvus-minio-data -o wide \
  | tee "$BACKUP_DIR/stage11-before/old-pvc-before-stage11.txt"

kubectl -n "$NS" get pvc milvus-data milvus-etcd-data milvus-minio-data -o yaml \
  > "$BACKUP_DIR/stage11-before/old-pvc-before-stage11.yaml"
```

验收：

- 文件 `deploy-milvus-etcd-before-stage11.yaml` 存在。
- 文件 `old-pvc-before-stage11.yaml` 存在。
- 旧 PVC `milvus-data`、`milvus-etcd-data`、`milvus-minio-data` 仍为 `Bound`。

### 11.4 检查本地仓库 YAML 是否已经按第 5.6 节改好

第 11 阶段不能在服务器上临时改 Pod 内文件，必须使用第 5 阶段在本地仓库准备好的 YAML/ConfigMap。执行前检查：

```bash
grep -R "claimName: $NEW_ETCD_PVC" k8s/05-milvus-etcd.yaml
grep -R "claimName: $NEW_MILVUS_PVC" k8s/07-milvus.yaml

grep -R "rag-kb" k8s/07-milvus.yaml k8s/07-milvus-config.yaml 2>/dev/null
grep -R "rootPath: milvus\|rootPath.*milvus\|bucketName: rag-kb\|bucketName.*rag-kb" \
  k8s/07-milvus.yaml k8s/07-milvus-config.yaml 2>/dev/null
```

如果这里查不到 `milvus-etcd-data-external-minio`、`milvus-data-external-minio`、`rag-kb`、`milvus`，说明第 5.6 节产物还没有准备好，不能继续。

对 Kubernetes 做 server-side dry-run：

```bash
kubectl -n "$NS" apply --dry-run=server -f k8s/05-milvus-etcd.yaml

if [ -f k8s/07-milvus-config.yaml ]; then
  kubectl -n "$NS" apply --dry-run=server -f k8s/07-milvus-config.yaml
fi

kubectl -n "$NS" apply --dry-run=server -f k8s/07-milvus.yaml
```

dry-run 通过后再继续。

### 11.5 停止旧 Milvus 和旧 etcd

停止旧 Milvus/etcd，避免旧服务继续写入旧对象存储或旧 etcd：

```bash
kubectl -n "$NS" scale deployment/milvus --replicas=0
kubectl -n "$NS" scale deployment/milvus-etcd --replicas=0

kubectl -n "$NS" rollout status deployment/milvus --timeout=600s || true
kubectl -n "$NS" rollout status deployment/milvus-etcd --timeout=600s || true
kubectl -n "$NS" get pods -l app=milvus -o wide
kubectl -n "$NS" get pods -l app=milvus-etcd -o wide
```

预期：

- `milvus` 和 `milvus-etcd` 的旧 Pod 已停止或正在被新配置替换。
- 不要删除旧 PVC。
- 不要执行 `etcdctl del "" --from-key=true`。
- 不要执行 `kubectl delete pvc milvus-data milvus-etcd-data milvus-minio-data`。

### 11.6 应用新 etcd PVC 和新 etcd workload

应用已经改好的 `k8s/05-milvus-etcd.yaml`。该文件必须创建或引用 `milvus-etcd-data-external-minio`。

```bash
kubectl -n "$NS" apply -f k8s/05-milvus-etcd.yaml
kubectl -n "$NS" rollout status deployment/milvus-etcd --timeout=600s
kubectl -n "$NS" get pvc milvus-etcd-data "$NEW_ETCD_PVC" -o wide
kubectl -n "$NS" get pods -l app=milvus-etcd -o wide
```

预期：

- 新 PVC `milvus-etcd-data-external-minio` 为 `Bound`。
- 旧 PVC `milvus-etcd-data` 仍存在。
- `milvus-etcd` Pod 正常运行，并挂载新 PVC。

如果实际对象是 StatefulSet 而不是 Deployment，将 rollout 命令改为：

```bash
kubectl -n "$NS" rollout status statefulset/milvus-etcd --timeout=600s
```

### 11.7 应用新 Milvus 配置和新 Milvus workload

如果第 5.6 节把 Milvus 配置拆成独立 ConfigMap，先应用配置：

```bash
if [ -f k8s/07-milvus-config.yaml ]; then
  kubectl -n "$NS" apply -f k8s/07-milvus-config.yaml
fi
```

再应用 Milvus workload。该文件必须让 Milvus 使用：

```text
bucketName=rag-kb
rootPath=milvus
etcd=milvus-etcd:2379
PVC=milvus-data-external-minio
```

执行：

```bash
kubectl -n "$NS" apply -f k8s/07-milvus.yaml
kubectl -n "$NS" rollout status deployment/milvus --timeout=600s
kubectl -n "$NS" get pvc milvus-data "$NEW_MILVUS_PVC" -o wide
kubectl -n "$NS" get pods -l app=milvus -o wide
```

预期：

- 新 PVC `milvus-data-external-minio` 为 `Bound`。
- 旧 PVC `milvus-data` 仍存在。
- Milvus Pod 正常运行。

### 11.8 验证新 Milvus 指向外部 MinIO

查看 Milvus 日志，确认它使用的是目标 bucket/rootPath，而不是旧的 `a-bucket/files`：

```bash
kubectl -n "$NS" logs deploy/milvus --tail=200 \
  | grep -Ei 'bucket|bucketName|bucketname|rootPath|root_path|root=|Minio|AwsChunkManager|remote chunk'
```

必须能确认：

```text
bucketName/rag-kb
rootPath/milvus
```

不能出现新实例继续使用以下旧配置：

```text
bucketname=a-bucket
root=files
address=milvus-minio:9000
```

检查外部 MinIO 目标路径：

```bash
mcli ls new-minio/rag-kb
mcli ls --recursive new-minio/rag-kb/milvus | head -20
```

预期：

- `rag-kb/milvus` 只包含新 Milvus 启动或后续 restore 产生的数据。
- 不应把旧 `a-bucket/files` 在线数据直接 mirror 到 `rag-kb/milvus`。
- Milvus backup 文件仍只放在 `rag-kb/milvus-backups/$MIGRATION_TS`。

### 11.9 验证新 Milvus 为空

在 API Pod 中使用 PyMilvus 连接当前 `milvus` Service。此时 API/worker 业务配置仍未切换，但可以借用 Pod 内 Python 环境做连通性检查：

```python
from pymilvus import connections, utility

connections.connect(host="milvus", port="19530")
print(utility.list_collections())
```

Kubernetes 执行方式：

```bash
kubectl -n "$NS" exec -i deploy/openrag-api -- python - <<'PY'
from pymilvus import connections, utility

connections.connect(host="milvus", port="19530")
print(utility.list_collections())
PY
```

验收标准：

- Milvus healthz 通过。
- 初始 collection 为空，输出应为 `[]`；如果存在集合，必须确认不是旧 etcd 元数据残留。
- 新 bucket 下没有混入旧 rootPath 数据。
- 旧 PVC `milvus-data`、`milvus-etcd-data`、`milvus-minio-data` 仍存在且未被删除。

如果 collection 不为空，或日志显示仍在使用 `a-bucket/files`、`milvus-minio:9000`，立即停止，不要进入阶段 12。

#### 11.10 当前执行结果（2026-05-22）

执行环境：`master1 / 172.16.22.13`，Namespace=`openrag`。

执行和验证记录：

- Pod 验证：`milvus` Pod 为 `Running`，`milvus-etcd` Pod 为 `Running`，均为 `1/1`，重启次数均为 `0`。截图中示例为 `milvus-5f6ccc5694-7jx5t` 运行在 `node3`，`milvus-etcd-559b44c4b9-qd9bz` 运行在 `node2`。
- PVC 验证：旧 PVC `milvus-data`、`milvus-etcd-data`、`milvus-minio-data` 均仍为 `Bound`；新 PVC `milvus-data-external-minio`、`milvus-etcd-data-external-minio` 均为 `Bound`。
- Deployment live spec 验证：`Deployment/milvus` 的 `data` volume 指向 `milvus-data-external-minio`，并挂载 `milvus-config` ConfigMap；`Deployment/milvus-etcd` 的 `data` volume 指向 `milvus-etcd-data-external-minio`。
- 外部 MinIO 路径验证：`new-minio/rag-kb` 下存在 `milvus-backups/` 与 `milvus/`；`new-minio/rag-kb/milvus` 下已出现 `index_files/`、`insert_log/` 等 Milvus 对象。

说明：由于阶段九恢复和索引补建已经在阶段八之后执行，现在不能再用“collection 为空”作为当前时点验收；阶段八当前时点验证以新 PVC、新配置、外部 MinIO 路径和旧 PVC 未删除为准。

结论：阶段八关键验收通过，新 Milvus 已基于新 PVC 和外部 MinIO `rag-kb/milvus` 路径运行，旧 PVC 保留。

## 12. 阶段九：恢复 Milvus

目的：把阶段 9/10 准备好的 Milvus backup 恢复到阶段 11 启动的空 Milvus 中。此时 API/worker 仍不切换，避免业务请求在恢复未完成时访问半成品向量库。

### 12.1 新 Milvus restore 配置

示例 `backup-restore.yaml`：

```yaml
milvus:
  address: <new-milvus-host>
  port: 19530
  authorizationEnabled: false
  tlsMode: 0

minio:
  storageType: minio
  address: 172.16.31.63
  port: 9000
  accessKeyID: raguser
  secretAccessKey: <REDACTED>
  useSSL: false
  useIAM: false

  bucketName: rag-kb
  rootPath: milvus

  backupBucketName: rag-kb
  backupRootPath: milvus-backups/<timestamp>
```

### 12.2 执行恢复

恢复原 collection 名称：

```bash
./milvus-backup --config backup-restore.yaml restore -n openrag-milvus-<timestamp>
```

如果需要先演练，可使用 suffix：

```bash
./milvus-backup --config backup-restore.yaml restore -n openrag-milvus-<timestamp> -s _recover
```

验收：

```python
from pymilvus import connections, utility, Collection

connections.connect(host="<new-milvus-host>", port="19530")
for name in utility.list_collections():
    c = Collection(name)
    print(name, c.num_entities)
```

验收标准：

- collection 名称符合预期。
- entity count 与阶段一基线一致。
- collection 可以 load。
- 抽样向量 search 能返回结果。
- API/worker 仍未应用第 5 阶段的新镜像和新配置，worker 仍保持停止或冻结状态。

#### 12.3 当前执行结果（2026-05-22）

执行环境：master1 / 172.16.22.13，Namespace=`openrag`，通过 `deploy/openrag-api` Pod 内 PyMilvus 连接 `milvus:19530`。

复验 collection 列表包含：

- `openrag_chunks`
- `openrag_layers`

初次复验 entity count 已匹配基线，但 `Collection.load()` 失败，错误为 `index not found`：

- `openrag_chunks`: entities=47979, expected=47979, match=True, load=FAIL index not found
- `openrag_layers`: entities=3022, expected=3022, match=True, load=FAIL index not found

排查发现两个 collection 的 `indexes:` 为空，字段结构中 `embedding` 为 `FLOAT_VECTOR`，dim=1024。

按应用代码期望为两个 collection 的 `embedding` 字段补建索引：

- `metric_type=COSINE`
- `index_type=IVF_FLAT`
- `params.nlist=128`

最终复验结果：

- `openrag_chunks`: entities=47979, expected=47979, match=True；indexes 包含 `('embedding', {'metric_type': 'COSINE', 'index_type': 'IVF_FLAT', 'params': {'nlist': 128}})`；load=ok。
- `openrag_layers`: entities=3022, expected=3022, match=True；indexes 包含 `('embedding', {'metric_type': 'COSINE', 'index_type': 'IVF_FLAT', 'params': {'nlist': 128}})`；load=ok。

结论：Milvus 数据和索引均已恢复到可加载状态，阶段九验收中 entity count 一致与 collection 可 load 已通过。抽样向量 search 本次未记录为已执行，后续阶段可继续补充验证。

## 13. 阶段十：迁移数据库 URL 字段

OpenRag 数据库里 `files.uri` 和 `document_chunks.object_key` 保持逻辑路径不变。

前置条件：

- 阶段 8 的业务对象已完成 mirror，且 `law` / `test` 的新旧对象数量和抽样对象大小一致。
- 阶段 12 的新 Milvus restore 已完成，collection/entity count 与阶段一 Milvus 基线一致。
- 第 5 阶段 `MinioStorage.path_style_http_url()` 的目标 URL 规则已经在测试环境验证。
- 生产 API/worker 仍未切换到新镜像和新 `STORAGE_*` 配置；URL rewrite 应发生在 API/worker 切换之前。

需要更新完整 URL 字段：

```text
files.l0_path
files.l1_path
files.l2_path
document_chunks.object_url
```

目标 URL 规则：

```text
http://172.16.31.63:9000/rag-kb/openrag/<workspace_slug>/<object_key>
```

该规则必须与改造后的 `MinioStorage.path_style_http_url` 完全一致。单桶模式下，`workspace.slug` 只作为逻辑 bucket 和 key 前缀使用，不能再出现在 URL 的 bucket 位置。

正确：

```text
http://172.16.31.63:9000/rag-kb/openrag/team-a/hierarchy/docs/a.pdf.abstract.md
```

错误：

```text
http://172.16.31.63:9000/team-a/hierarchy/docs/a.pdf.abstract.md
```

在执行 UPDATE 前先确认 `document_chunks.object_url` 的实际存储情况。当前 `document_processor.py` 会把 `object_url` 写入数据库，但不同历史数据可能存在 null：

```sql
select
  case when object_url is null then 'null' else 'has_value' end as url_status,
  count(*) as rows
from document_chunks
group by url_status
order by url_status;
```

如果大量为 null，说明这些行不需要做 URL rewrite；后续接口如改为动态生成 URL，会由新的 `path_style_http_url` 逻辑自然返回新地址。若 `object_url` 已有值，则需要 UPDATE 静态值。

执行建议：

1. 先生成 dry-run SQL 或临时映射表。
2. 抽样确认旧 URL 能正确映射到新 URL。
3. 在事务内更新。
4. 更新后抽样访问。
5. 记录受影响行数，并把执行 SQL 保存到迁移记录中。

示例查询：

```sql
select f.id, w.slug, f.uri, f.l0_path, f.l1_path, f.l2_path
from files f
join workspaces w on w.id = f.workspace_id
where f.l0_path is not null or f.l1_path is not null or f.l2_path is not null
limit 20;

select dc.id, w.slug, dc.object_key, dc.object_url
from document_chunks dc
join workspaces w on w.id = dc.workspace_id
where dc.object_url is not null
limit 20;
```

验收标准：

- `files.l0_path/l1_path/l2_path` 指向新 endpoint 和 `rag-kb/openrag/<workspace_slug>/...`。
- `document_chunks.object_url` 指向新 endpoint 和 `rag-kb/openrag/<workspace_slug>/...`。
- 抽样 URL 可访问。
- 搜索接口返回的 chunk URL 为新 URL。
- API/worker 尚未切换前，如需业务访问，应继续保持维护或冻结状态，避免旧代码生成旧 URL。

#### 13.1 当前执行结果（2026-05-22）

执行环境：

```text
执行节点: master1 / 172.16.22.13
Namespace: openrag
Postgres Pod: postgres-0
数据库: openrag
用户: openrag
```

执行前验证发现数据库 URL 处于半迁移状态：已有 endpoint 是 `http://172.16.31.63:9000`，但旧形态仍为 `http://172.16.31.63:9000/<workspace_slug>/...`，缺少目标路径中的 `rag-kb/openrag/<workspace_slug>/...`。

dry-run 计数：

| 字段 | workspace | 需改写行数 |
| --- | --- | ---: |
| files.l0_path/l1_path/l2_path | law | 1408 |
| files.l0_path/l1_path/l2_path | test | 2 |
| document_chunks.object_url | law | 44606 |
| document_chunks.object_url | test | 172 |

实际事务执行结果：

```text
UPDATE 1410
UPDATE 44778
COMMIT
```

复验结果：

| 字段 | non_null | new_url | not_new |
| --- | ---: | ---: | ---: |
| files.l0_path | 1410 | 1410 | 0 |
| files.l1_path | 1410 | 1410 | 0 |
| files.l2_path | 1410 | 1410 | 0 |
| document_chunks.object_url | 44778 | 44778 | 0 |

结论：数据库 URL 字段已完成阶段十目标 URL rewrite，静态 URL 均指向 `http://172.16.31.63:9000/rag-kb/openrag/<workspace_slug>/...`。如需更严格验收，后续阶段可继续抽样 `mcli stat` 或通过 API 搜索结果验证对象可访问。

## 14. 阶段十一：切换 API 和 worker

目的：把第 5 阶段产出的新 API/worker 镜像和 Kubernetes YAML 正式应用到生产，使业务服务开始读取外部 MinIO 的 `rag-kb/openrag/...` 对象，并连接已恢复完成的新 Milvus。

这是生产切换点。阶段 5 只是准备和验证产物；真正替换生产 API/worker 必须在本阶段执行。

前置条件：

- 阶段 6 已冻结写入，worker 仍保持停止或冻结状态。
- 阶段 7 已保存旧 Deployment/ConfigMap/Secret/镜像 tag/运行环境变量。
- 阶段 8 业务对象已迁移到 `rag-kb/openrag/law` 和 `rag-kb/openrag/test`，对象数量和抽样校验通过。
- 阶段 12 新 Milvus restore 成功，`openrag_chunks=47979`、`openrag_layers=3022` 或与最终确认基线一致。
- 阶段 13 URL rewrite 已完成，静态 URL 字段指向 `rag-kb/openrag/<workspace_slug>/...`。
- 第 5 阶段新 API/worker/web 镜像已通过镜像仓库，或在无 Harbor/Registry 环境下通过全节点离线导入方式完成分发；对应 YAML 需在正式 apply 前完成 dry-run 验证。

#### 14.0 当前前置执行结果（2026-05-23）

当前环境确认没有 Harbor/Registry，因此阶段十一的镜像分发方式采用离线包全节点导入，而不是推送镜像仓库。

本次需要覆盖的 4 台部署节点如下。后续凡是涉及镜像分发、镜像存在性检查、节点级 Docker/containerd 操作，都必须覆盖完整清单，避免遗漏单个节点：

| 序号 | 服务器地址 | 当前用途/状态 |
| --- | --- | --- |
| 1 | `172.16.22.13` | master1/当前主要操作节点；已完成离线镜像 `docker load` |
| 2 | `172.16.31.51` | Kubernetes 部署节点；已完成离线镜像 `docker load` |
| 3 | `172.16.31.52` | Kubernetes 部署节点；已完成离线镜像 `docker load` |
| 4 | `172.16.31.53` | Kubernetes 部署节点；已完成离线镜像 `docker load` |

建议后续命令统一使用：

```bash
export NODES="172.16.22.13 172.16.31.51 172.16.31.52 172.16.31.53"
```

本地已传入内网服务器的产物清单：

| 本地产物 | 内网保存位置 | 用途/备注 |
| --- | --- | --- |
| `openrag-offline-1.1.0-external-minio-images.tar` | `/root/lisiqi/openrag-migration/data/openrag-offline-1.1.0-external-minio-images.tar` | 离线镜像包；用于无 Harbor 环境下在每台节点执行 `docker load` |
| `openrag-offline-1.1.0-external-minio-images.tar.sha256` | `/root/lisiqi/openrag-migration/data/openrag-offline-1.1.0-external-minio-images.tar.sha256` | 离线包 SHA256 校验文件；已校验返回 `OK` |
| `k8s/00-namespace.yaml` | `/root/lisiqi/openrag-migration/data/k8s/00-namespace.yaml` | Kubernetes namespace manifest |
| `k8s/01-secret.yaml` | `/root/lisiqi/openrag-migration/data/k8s/01-secret.yaml` | 生产 Secret manifest；内容已从本地同步到内网 |
| `k8s/01-secret.example.yaml` | `/root/lisiqi/openrag-migration/data/k8s/01-secret.example.yaml` | Secret 示例文件；仅作参考，不应用到生产 |
| `k8s/02-configmap-nginx.yaml` | `/root/lisiqi/openrag-migration/data/k8s/02-configmap-nginx.yaml` | Nginx ConfigMap manifest |
| `k8s/03-postgres.yaml` | `/root/lisiqi/openrag-migration/data/k8s/03-postgres.yaml` | Postgres manifest |
| `k8s/05-milvus-etcd.yaml` | `/root/lisiqi/openrag-migration/data/k8s/05-milvus-etcd.yaml` | Milvus etcd manifest |
| `k8s/07-milvus-config.yaml` | `/root/lisiqi/openrag-migration/data/k8s/07-milvus-config.yaml` | Milvus 配置 manifest；应用前必须确认外部 MinIO secret 占位符已替换 |
| `k8s/07-milvus.yaml` | `/root/lisiqi/openrag-migration/data/k8s/07-milvus.yaml` | Milvus deployment/service manifest |
| `k8s/08-elasticsearch.yaml` | `/root/lisiqi/openrag-migration/data/k8s/08-elasticsearch.yaml` | Elasticsearch manifest |
| `k8s/09-api.yaml` | `/root/lisiqi/openrag-migration/data/k8s/09-api.yaml` | OpenRag API manifest；应用前需确认镜像 tag 为 `openrag/api:1.1.0` |
| `k8s/10-task-worker.yaml` | `/root/lisiqi/openrag-migration/data/k8s/10-task-worker.yaml` | OpenRag worker manifest；应用前需确认镜像 tag 为 `openrag/task-worker:1.1.0` |
| `k8s/11-web.yaml` | `/root/lisiqi/openrag-migration/data/k8s/11-web.yaml` | OpenRag web manifest；应用前需确认镜像 tag 为 `openrag/web:1.1.0` |
| `k8s/12-ingress.yaml` | `/root/lisiqi/openrag-migration/data/k8s/12-ingress.yaml` | Ingress manifest |
| `k8s/13-configmap-openrag-llm.yaml` | `/root/lisiqi/openrag-migration/data/k8s/13-configmap-openrag-llm.yaml` | OpenRag LLM ConfigMap manifest |
| `k8s/14-configmap-openrag-web-runtime.yaml` | `/root/lisiqi/openrag-migration/data/k8s/14-configmap-openrag-web-runtime.yaml` | OpenRag web runtime ConfigMap manifest |
| `k8s/15-configmap-openrag-service-conf.yaml` | `/root/lisiqi/openrag-migration/data/k8s/15-configmap-openrag-service-conf.yaml` | OpenRag service config manifest；应用前必须确认外部 MinIO secret 占位符已替换 |
| `k8s/kustomization.yaml` | `/root/lisiqi/openrag-migration/data/k8s/kustomization.yaml` | Kustomize 入口；可用于整体 dry-run/apply |

已完成事项：

- 离线镜像包 `openrag-offline-1.1.0-external-minio-images.tar` 已上传到内网服务器目录 `/root/lisiqi/openrag-migration/data/`。
- 校验文件 `openrag-offline-1.1.0-external-minio-images.tar.sha256` 已一并上传。
- 已执行 `sha256sum -c openrag-offline-1.1.0-external-minio-images.tar.sha256`，结果为 `openrag-offline-1.1.0-external-minio-images.tar: OK`。
- 离线镜像包已分发并在 4 台 Kubernetes 节点上完成 `docker load`。
- 已导入的关键镜像包括：
  - `openrag/api:1.1.0`
  - `openrag/task-worker:1.1.0`
  - `openrag/web:1.1.0`
  - `postgres:16-alpine`
  - `quay.io/coreos/etcd:v3.5.5`
  - `milvusdb/milvus:v2.4.17`
  - `docker.elastic.co/elasticsearch/elasticsearch:8.12.2`
  - `busybox:1.36`

阶段十一镜像分发前置条件已满足。以下是进入正式 apply 前的检查项；其中 2026-05-23 已完成的 dry-run 状态见后续“当前 dry-run 记录”。

- 确认 `k8s/09-api.yaml`、`k8s/10-task-worker.yaml`、`k8s/11-web.yaml` 的镜像 tag 已指向 `1.1.0`。
- 确认无 `imagePullPolicy: Always`；如存在，应改为 `IfNotPresent`。
- 确认 `k8s/07-milvus-config.yaml` 和 `k8s/15-configmap-openrag-service-conf.yaml` 中不再存在 `CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY`。
- 执行 `kubectl apply --dry-run=server` 验证待发布 YAML。

#### 14.0.1 当前 dry-run 记录（2026-05-23）

用户已在内网 `master1 / 172.16.22.13` 的 `/root/lisiqi/openrag-migration/data` 目录创建并使用渲染目录：

```text
k8s-rendered-stage11-20260523101628
```

该渲染目录用于阶段十一正式 apply 前的发布文件准备，不直接修改仓库原始 `k8s/` 清单。实际发布文件检查结果如下：

- `09-api.yaml` 应使用 `openrag/api:1.1.0`。
- `10-task-worker.yaml` 应使用 `openrag/task-worker:1.1.0`。
- `11-web.yaml` 应使用 `openrag/web:1.1.0`。
- `09-api.yaml`、`10-task-worker.yaml`、`11-web.yaml` 的 `imagePullPolicy` 应为 `IfNotPresent`。
- 限定到实际发布文件后，`CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY`、`imagePullPolicy: Always`、`openrag/(api|task-worker|web):1.0.0` 检查已通过。

全目录检查曾出现两类可解释命中：

- `11-web.yaml.bak` 中仍有 `imagePullPolicy: Always`。这是 `sed -i.bak` 产生的备份文件，不是发布目标文件。
- `01-secret.example.yaml` 中仍有 `CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY`。这是示例文件，不应 apply 到生产。

实际发布文件 server-side dry-run 已通过，输出包括：

```text
secret/openrag-secrets configured (server dry run)
configmap/milvus-config configured (server dry run)
configmap/openrag-service-conf created (server dry run)
persistentvolumeclaim/openrag-api-uploads unchanged (server dry run)
service/api unchanged (server dry run)
deployment.apps/openrag-api configured (server dry run)
deployment.apps/openrag-task-worker configured (server dry run)
service/web unchanged (server dry run)
deployment.apps/openrag-web configured (server dry run)
```

重要边界：

- 截至本记录，正式生产 `kubectl apply` 尚未执行。
- API、worker、web 尚未据此记录为已完成切换。
- 渲染目录中包含已替换真实外部 MinIO Secret 的文件，后续记录不得写出真实 Secret。
- `k8s-rendered-stage11-20260523101628` 和后续切换前快照目录都应按敏感制品处理，不应提交到 Git 或外传。

### 14.1 新 OpenRag 存储配置

生产 API/worker 应使用第 5 阶段 YAML 中的 `STORAGE_` 环境变量：

```text
STORAGE_TYPE=minio
STORAGE_ENDPOINT=http://172.16.31.63:9000
STORAGE_ACCESS_KEY=raguser
STORAGE_SECRET_KEY=<REDACTED>
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
STORAGE_PUBLIC_URL=http://172.16.31.63:9000
```

### 14.2 旧 RAGFlow 存储配置

`RAGFlowMinio` 应通过第 5 阶段准备的 `conf/service_conf.yaml` 或等价 ConfigMap 挂载读取：

```yaml
minio:
  host: 172.16.31.63:9000
  user: raguser
  password: <REDACTED>
  bucket: rag-kb
  prefix_path: openrag
  secure: false
```

并设置：

```text
STORAGE_IMPL=MINIO
```

### 14.3 Milvus 客户端配置

API 和 worker 作为 Milvus 客户端，只需要连接新的 Milvus service：

```text
MILVUS_HOST=<new-milvus-service>
MILVUS_PORT=19530
```

如果阶段 11 复用了原 Service 名称 `milvus`，则 API/worker 可继续使用：

```text
MILVUS_HOST=milvus
MILVUS_PORT=19530
```

Milvus server 自身的 MinIO bucket/rootPath 配置通过第 5.6 节的 `milvus.yaml` 注入，不应只依赖 API/worker 环境变量。

### 14.4 部署第 5 阶段产物

截至 2026-05-23，阶段十一发布文件已通过 server-side dry-run，但正式生产 apply 尚未执行。正式切换时应先保存切换前快照，再应用 Secret/ConfigMap，再只滚动 API 并验证；API 验证通过后滚动 Web，最后恢复或滚动 worker。

切换前先保存当前集群状态，快照目录同样按敏感制品处理：

```bash
export NS=openrag
SNAPSHOT_DIR="stage11-before-apply-$(date +%Y%m%d%H%M%S)"
mkdir -p "$SNAPSHOT_DIR"

kubectl -n "$NS" get deploy openrag-api openrag-task-worker openrag-web -o yaml > "$SNAPSHOT_DIR/deployments-before.yaml"
kubectl -n "$NS" get cm milvus-config openrag-service-conf openrag-web-runtime-config -o yaml > "$SNAPSHOT_DIR/configmaps-before.yaml"
kubectl -n "$NS" get secret openrag-secrets -o yaml > "$SNAPSHOT_DIR/secret-before.yaml"
kubectl -n "$NS" get pods -o wide > "$SNAPSHOT_DIR/pods-before.txt"
```

然后应用 Secret/ConfigMap，注意这里应使用阶段十一渲染目录中的实际发布文件：

```bash
export NS=openrag
export RENDER_DIR=k8s-rendered-stage11-20260523101628

kubectl -n "$NS" apply -f "$RENDER_DIR/01-secret.yaml"
kubectl -n "$NS" apply -f "$RENDER_DIR/07-milvus-config.yaml"
kubectl -n "$NS" apply -f "$RENDER_DIR/15-configmap-openrag-service-conf.yaml"
```

先滚动 API：

```bash
kubectl -n "$NS" apply -f "$RENDER_DIR/09-api.yaml"
kubectl -n "$NS" rollout status deployment/openrag-api --timeout=600s

kubectl -n "$NS" exec deploy/openrag-api -- env | grep -Ei 'STORAGE_(TYPE|ENDPOINT|BUCKET|PREFIX|PUBLIC_URL)|MILVUS_HOST|MILVUS_PORT|STORAGE_IMPL'
```

API 基础验证通过后，再滚动 Web：

```bash
kubectl -n "$NS" apply -f "$RENDER_DIR/11-web.yaml"
kubectl -n "$NS" rollout status deployment/openrag-web --timeout=600s
```

最后恢复或滚动 worker：

```bash
kubectl -n "$NS" apply -f "$RENDER_DIR/10-task-worker.yaml"
kubectl -n "$NS" rollout status deployment/openrag-task-worker --timeout=600s

kubectl -n "$NS" exec deploy/openrag-task-worker -- env | grep -Ei 'STORAGE_(TYPE|ENDPOINT|BUCKET|PREFIX|PUBLIC_URL)|MILVUS_HOST|MILVUS_PORT|STORAGE_IMPL'
```

如果阶段 6 中手工将 worker 缩容为 0，且 `k8s/10-task-worker.yaml` 中保留了期望副本数，则 `apply` 会恢复副本；否则手工恢复：

```bash
kubectl -n "$NS" scale deployment/openrag-task-worker --replicas=<expected-replicas>
kubectl -n "$NS" rollout status deployment/openrag-task-worker --timeout=600s
```

#### 14.4.1 worker 切换/恢复后异常观察（2026-05-23）

本记录为阶段十一切换后的异常观察，不等同于阶段十一切换验收通过。用户已完成 worker 切换/恢复后的验证，worker 环境变量显示：

```text
STORAGE_BUCKET=rag-kb
STORAGE_PREFIX=openrag
STORAGE_ENDPOINT=http://172.16.31.63:9000
MILVUS_HOST=milvus
image=openrag/task-worker:1.1.0
```

异常现象：

- worker 日志出现 S3 `NoSuchKey`，错误信息包含 `Object does not exist`。
- 日志中的 `bucket_name=rag-kb`。
- 日志中的 `object_name` 形如 `openrag/law/regulations/...`。
- 日志中的 `resource` 形如 `/rag-kb/openrag/law/...`。
- 示例任务包括 `Task 1204`、`Task 1625`；随后仍有 `Task 786`、`Task 1198` 处于 `executing`。

处理边界：

- 这条记录仅说明 worker 已切到新镜像和新环境后观察到对象缺失异常，不能作为阶段十一验收通过依据。
- 在缺失对象根因定位前，需要暂停或谨慎处理 worker 后续批量任务，避免继续放大失败任务量或数据不一致。

下一步建议：

- 检查 `new-minio/rag-kb/openrag/law` 下实际对象路径。
- 对失败日志中的具体 `object_name` 执行 `mcli stat new-minio/rag-kb/<object_name>`。
- 与 `old-minio/law` 对比，判断是 mirror 漏对象、路径编码/空格差异，还是数据库引用与对象迁移结果不一致。

#### 14.4.2 NoSuchKey 失败对象三方对照排查记录（2026-05-23）

本节记录 NoSuchKey 后续排查进展，不作为阶段十一验收通过依据。根因未闭环前，worker 应保持缩容/冻结，不应继续放量处理后续批量任务。

失败对象示例：

```text
OBJ=openrag/law/regulations/sse/rules/本所业务规则/repeal/已废止规则文本/上海证券交易所科创板上市公司证券发行上市审核规则.doc
```

旧 MinIO 对照使用的 `REL` 为去掉 `openrag/law/` 后的相对路径，即形如：

```text
regulations/sse/rules/本所业务规则/repeal/已废止规则文本/上海证券交易所科创板上市公司证券发行上市审核规则.doc
```

三方对照结果：

- 新目标 MinIO：`mcli stat new-minio/rag-kb/$OBJ` 返回 `Object does not exist`，确认新 MinIO 目标路径缺对象。
- 旧在线 MinIO：检查 `old-minio/law/$REL` 时出现 `http://127.0.0.1:19000/law/?location= dial tcp 127.0.0.1:19000 connect refused`。这说明 `old-minio` alias 依赖的 `kubectl port-forward` / 本地 `19000` 端口当前未运行，或旧 MinIO 服务不可达；因此尚不能判定旧在线 MinIO 是否有该对象。
- 旧在线 MinIO 命令输出还出现 `old-minio stat object does not exist`，但必须在恢复 old-minio 连接后重试确认，不能在连接拒绝状态下直接判定源对象不存在。
- 本地备份：`find` 本地备份命令未显示匹配输出（以截图为准），尚未确认备份中是否存在该对象。
- 后续补充：旧 MinIO `port-forward` 恢复后，`mcli stat/find` 证明旧在线 MinIO `old-minio/law` 中存在该失败对象：`old-minio/law/regulations/sse/rules/本所业务规则/repeal/已废止规则文本/上海证券交易所科创板上市公司证券发行上市审核规则.doc`。
- 该对象元数据：大小 `79 KiB`，`Content-Type=application/msword`，`ETag=888896054f4c7fb7bf30238ec5e0ab86`。
- 与之相对，此前 `new-minio/rag-kb/openrag/law/...` 同一路径缺失，`mcli stat new-minio/rag-kb/$OBJ` 返回 `Object does not exist`。

当前结论：

- worker 已成功缩容到 `0`，当前 `openrag-task-worker` Pod 无资源。
- 至少对上述样本而言，`NoSuchKey` 更像业务对象 mirror/补同步缺失，而不是数据库凭空引用。
- 阶段十一切换后验收仍不能记为通过。
- worker 应继续保持缩容/冻结，直到补同步和复验完成，避免继续放大失败任务和数据不一致面。

下一步建议：

- 主流程补同步 `old-minio/law` 到 `new-minio/rag-kb/openrag/law`。
- 补同步后复验该失败对象，至少执行 `mcli stat new-minio/rag-kb/$OBJ`，并与旧 MinIO 的大小、Content-Type、ETag 或可接受的校验口径对照。
- 补同步和复验完成前，不恢复 worker 放量，也不把阶段十一验收记为通过。

#### 14.4.3 law 补同步后 NoSuchKey 样本复验记录（2026-05-23）

本节记录 `law` 业务对象补同步后的样本复验结果，不阻塞主流程；该结果只说明至少一个已知 NoSuchKey 样本已被补同步修复，仍不能作为阶段十一整体验收通过依据。

补同步操作：

```bash
mcli mirror --overwrite old-minio/law new-minio/rag-kb/openrag/law
```

补同步后，用户重新执行以下复验命令已成功：

```bash
mcli stat new-minio/rag-kb/$OBJ
```

复验成功的样本对象元数据：

```text
Name: 上海证券交易所科创板上市公司证券发行上市审核规则.doc
Size: 79 KiB
ETag: 888896054f4c7fb7bf30238ec5e0ab86
Content-Type: application/msword
```

对照结论：

- 该样本在新 MinIO 的 ETag 和大小与旧 MinIO 样本一致。
- 至少上述 `NoSuchKey` 样本已由 `law` 补同步修复。
- worker 仍应保持缩容/冻结，不能因单个样本复验成功就恢复放量。

下一步要求：

- 对比 `old-minio/law` 与 `new-minio/rag-kb/openrag/law` 的对象数量。
- 对 `law` 做必要抽样，覆盖原始文件和 hierarchy/chunk 类对象。
- 对比 `old-minio/test` 与 `new-minio/rag-kb/openrag/test` 的对象数量。
- 如 `test` 存在缺失，补同步 `test` 到 `new-minio/rag-kb/openrag/test` 并抽样复验。
- 上述对象数量对比、必要抽样和 `test` 补同步完成后，才考虑恢复 worker。

#### 14.4.4 law/test 业务对象补同步完成度记录（2026-05-23）

本节记录 NoSuchKey 后续对象补同步完成度，不记录任何 Secret，不作为阶段十一整体验收通过依据。

已完成的补同步和数量对比：

- `law`：用户已完成业务对象补同步和数量对比，`mcli ls --recursive old-minio/law | wc -l = 48994`，`mcli ls --recursive new-minio/rag-kb/openrag/law | wc -l = 48994`。
- `test`：用户执行 `mcli mirror --overwrite old-minio/test new-minio/rag-kb/openrag/test` 后完成数量对比，`mcli ls --recursive old-minio/test | wc -l = 178`，`mcli ls --recursive new-minio/rag-kb/openrag/test | wc -l = 178`。

当前结论：

- `law` 和 `test` 业务对象数量已与阶段一旧 MinIO 基线一致。
- 之前用于定位 NoSuchKey 的样本对象已在新 MinIO 目标路径 `stat` 成功。
- 业务对象补同步数量口径已经闭环，但仍需要通过 worker 恢复后的日志观察确认运行面无新增缺失对象。

下一步主流程：

- 谨慎恢复 worker `1` 副本并观察日志。
- 如果仍出现 `NoSuchKey`，继续按失败日志中的具体 `object_name` 逐个执行 `mcli stat new-minio/rag-kb/<object_name>` 定位。

#### 14.4.5 worker 1 副本观察窗口记录（2026-05-23）

本节记录阶段十一恢复 worker 后的正向观察，不阻塞主流程；该记录不作为阶段十一最终验收完成依据。

观察操作：

```bash
kubectl -n openrag logs -f deploy/openrag-task-worker
```

用户恢复 `openrag-task-worker` 为 `1` 副本后执行上述日志观察，并反馈“观察日志无异常”。

观察结论：

- 补同步 `law`/`test` 后，worker `1` 副本观察窗口内未再看到 `NoSuchKey`、`S3 operation failed`、`Object does not exist`、`Traceback` 等异常。
- 这是阶段十一恢复 worker 的正向观察结果。
- 阶段十一最终验收仍不能据此标记为完成，除非用户后续确认业务验收完成。

后续仍建议继续完成业务功能验收：

- 旧文件预览、下载、检索。
- 新上传对象落点。
- 任务堆积检查。
- API/worker 当前镜像和环境变量检查。

#### 14.4.6 worker 恢复后日志过滤与 tasks 状态检查（2026-05-23）

本节记录阶段十一 worker 恢复后的任务状态检查，不阻塞主流程；该记录不作为阶段十一业务验收最终完成依据。

日志过滤操作：

```bash
kubectl -n openrag logs deploy/openrag-task-worker --tail=500 | grep -Ei 'NoSuchKey|S3 operation failed|Object does not exist|Traceback|Exception|ERROR' || true
```

日志过滤结果：

- 输出只有 Kubernetes 的 `Defaulted container` 提示。
- 未见 `NoSuchKey`、`S3 operation failed`、`Object does not exist`、`Traceback`、`Exception`、`ERROR` 命中。

tasks 状态查询结果：

- `cancelled=1`
- `failure=166`
- `success=1413`

当前判断：

- 该结果说明 worker 当前日志观察无明显新增对象读取异常。
- `failure=166` 是否为历史遗留或刚才切换过程中新增，尚需按 `tasks` 时间字段和错误字段追查。
- 阶段十一业务验收仍未最终完成。

下一步主流程：

- 查询 `tasks` 表字段。
- 查询最近失败任务详情，按时间和错误内容确认 `failure=166` 的来源。

#### 14.4.7 tasks/files 排查记录与 S3 missing failure 处置边界（2026-05-23）

本节记录阶段十一 worker 恢复后的 `tasks` / `files` 深入排查结果，不阻塞主流程；该记录不作为阶段十一业务验收最终完成依据。

查询时间：

- 用户查询数据库时间为 `2026-05-23 03:04:31 UTC`。

tasks 排查结果：

- 最近 2 小时 tasks 状态统计只有 `failure=161`。
- 最近失败任务详情显示，多个 `failure` 任务创建时间集中在 `2026-04-30 01:34/01:35` 左右。
- 错误内容均为 `S3 operation failed` / `NoSuchKey` / `Object does not exist`。
- 失败 `resource` 指向 `/rag-kb/openrag/law/regulations/...`。
- S3 缺对象失败统计为 `s3_missing_failures=161`。

files 当前统计：

| workspace | file_rows | completed_documents | failed_documents |
| --- | ---: | ---: | ---: |
| law | 1573 | 1406 | 166 |
| test | 3 | 2 | 0 |

与阶段一基线对比：

- 阶段一基线中 `law failed_documents=116`。
- 当前 `law failed_documents=166`，说明切换/恢复 worker 期间至少有新增失败文件，或历史任务被重新标记为失败。

当前结论：

- `law` / `test` 对象补同步已完成，但数据库里的失败任务状态和文件失败状态不会自动恢复。
- 阶段十一仍未最终验收通过。

下一步处置边界：

- 先确认 `NoSuchKey` 失败样本对象现已存在。
- 再决定是否批量 retry/reset 这批 S3 missing failure 任务及对应文件状态。

#### 14.4.8 S3 failure recovery SQL 后 worker 重跑状态变化记录（2026-05-23）

本节记录 `migration_stage11_s3_failure_recovery_20260523` 相关恢复任务重跑后的状态变化，不阻塞主流程；该记录不作为阶段十一业务验收最终完成依据。

已发生事实：

- S3 failure recovery SQL 已提交。
- worker 被恢复运行，并处理了一部分 `pending` 任务。

tasks 当前状态：

- `cancelled=1`
- `failure=39`
- `pending=61`
- `started=2`
- `success=1477`

刚重置后的对照状态：

- `cancelled=1`
- `failure=5`
- `pending=161`
- `success=1413`

状态变化：

- `success` 增加 `64`。
- `pending` 减少 `100`。
- `failure` 增加 `34`。
- 当前仍有 `2` 个任务处于 `started`。

当前判断：

- worker 已经推进了一部分重跑任务。
- 但新增 `failure` 需要继续确认错误类型，不能直接判定 S3 failure recovery 已闭环。
- 阶段十一仍不能标记为最终完成。

下一步主流程：

- 先暂停 worker。
- 检查 `migration_stage11_s3_failure_recovery_20260523` 这批任务中当前失败的错误类型。
- 确认失败是否仍为 S3 `NoSuchKey` / `Object does not exist`，还是已经转为其他解析错误或业务错误。

#### 14.4.9 S3 failure recovery 错误类型收敛记录（2026-05-23）

本节记录 `migration_stage11_s3_failure_recovery_20260523` 批次当前失败类型收敛结论，不阻塞主流程；该记录不作为阶段十一业务验收最终完成依据。

批次当前状态：

- `failure=41`
- `pending=33`
- `started=2`
- `success=85`

错误类型统计：

- 当前 `failure` 中 `error_type=other` 为 `41`。
- 没有 `s3_missing` 命中。
- 没有 `s3_operation` 命中。

失败样本错误内容：

```text
OCR local model init failed and OPENRAG_DISABLE_HF_DOWNLOAD is enabled
```

收敛结论：

- 对象补同步后，当前新增失败已从 MinIO/S3 `NoSuchKey` 问题转为 OCR/解析模型环境问题。
- 这批当前失败不再属于外部 MinIO 迁移路径缺对象问题。
- 阶段十一仍需完成业务验收，但 MinIO `NoSuchKey` 根因已收束。

主流程建议：

- 不继续用 MinIO 迁移流程处理 OCR 失败。
- 先停止 worker。
- 等待 `started` 任务收敛，或按策略将其恢复为 `pending`。
- 之后再做迁移验收。
- OCR 模型环境问题应单独记录和处理，不与外部 MinIO 迁移路径缺对象问题混在同一处置链路中。

#### 14.4.10 S3 failure recovery 批次完成和问题收束记录（2026-05-23）

本节记录 `migration_stage11_s3_failure_recovery_20260523` 批次完成后的收束结论，不阻塞主流程；该记录不作为阶段十一业务验收最终完成依据。

恢复批次观察：

- 用户恢复 worker 后使用固定循环观察 recovery 批次。
- `active_recovery_tasks` 从 `35` 逐步下降到 `0`。
- 观察窗口为 `2026-05-23 12:14:10 +08:00` 到 `2026-05-23 12:23:11 +08:00`。

批次最终状态：

- `failure=61`
- `success=100`

失败类型统计：

- `ocr_model=59`
- `other=2`
- 没有 `s3_missing` 命中。
- 没有 `s3_operation` 命中。

worker 日志复核：

- 最近 30 分钟 worker 日志 grep `NoSuchKey` 无命中。
- 最近 30 分钟 worker 日志 grep `S3 operation failed` 无命中。
- 最近 30 分钟 worker 日志 grep `Object does not exist` 无命中。
- 日志输出仅有 Kubernetes `Defaulted container` 提示。

收束结论：

- MinIO/S3 `NoSuchKey` 缺对象问题已通过补同步和恢复批次处理收束。
- 剩余失败主要为 `OCR local model init failed and OPENRAG_DISABLE_HF_DOWNLOAD is enabled`。
- 该剩余问题属于 OCR/解析模型环境问题，不应继续按 MinIO 迁移问题处理。
- 阶段十一还需完成业务冒烟验收后才能最终标记完成。

### 14.5 切换后验证

先验证旧数据读取，再验证新写入：

```bash
mcli ls --recursive new-minio/rag-kb/openrag/law | head
mcli ls --recursive new-minio/rag-kb/openrag/test | head
```

验收：

- 老文件预览成功。
- 老文件下载成功。
- 老文件语义检索成功。
- 老文件 hierarchical 检索成功。
- 上传新文件成功。
- 新原始文件落到 `rag-kb/openrag/<workspace_slug>/...`。
- 新 hierarchy 对象落到 `rag-kb/openrag/<workspace_slug>/hierarchy/...`。
- 新向量数据落到 `rag-kb/milvus/...`。
- 任务表无异常堆积。
- 旧 workspace bucket `old-minio/law`、`old-minio/test` 中不再出现切换后的新对象。
- API/worker 当前镜像 tag 与第 5 阶段发布记录一致。

## 15. 回滚方案

在观察期内保留：

- 旧 Postgres dump。
- 旧 MinIO 对象级备份和旧 MinIO 在线数据。
- 旧 Milvus backup 文件，包括旧 MinIO 内的 `a-bucket/backup/<timestamp>` 和新 MinIO 内的 `rag-kb/milvus-backups/<timestamp>`。
- 旧 Milvus/etcd/MinIO PVC 原样保留；本环境无 `VolumeSnapshotClass`，没有 PVC 快照可用于恢复。
- 阶段 7.0 保存的旧 Deployment/StatefulSet/ConfigMap/Secret YAML。
- 阶段 7.0 保存的旧 API/worker 镜像 tag 和运行环境变量。
- 第 5 阶段发布的新镜像 tag 和 YAML，用于定位切换版本。

回滚分界：

- 如果失败发生在阶段 14 前，优先停止继续迁移，不应用第 5 阶段新 API/worker 产物；按已执行阶段回退数据或继续修复。
- 如果失败发生在阶段 14 后，必须同时处理 API/worker 镜像与配置、Milvus 指向、数据库 URL 字段和可能的新写入对象。

回滚路径：

1. 停止新 worker：

```bash
kubectl -n "$NS" scale deployment/openrag-task-worker --replicas=0
```

2. API 进入维护模式，或临时阻断上传、删除、移动、重处理入口。
3. 恢复 API/worker 旧镜像和旧配置。优先使用阶段 7.0 保存的 YAML：

```bash
kubectl -n "$NS" apply -f migration-backup-<timestamp>/deployments-before-minio-migration.yaml
kubectl -n "$NS" rollout status deployment/openrag-api --timeout=600s
kubectl -n "$NS" rollout status deployment/openrag-task-worker --timeout=600s
```

如果只需要快速回滚镜像，也可使用 Kubernetes rollout 历史，但必须确认环境变量也一并回到旧值：

```bash
kubectl -n "$NS" rollout undo deployment/openrag-api
kubectl -n "$NS" rollout undo deployment/openrag-task-worker
```

4. 恢复旧 Milvus 指向：
   - 阶段 11 必须使用新 Milvus PVC 和新 etcd PVC，因此回滚时恢复阶段 7.0/11.3 保存的旧 Milvus/etcd Deployment YAML，让它们重新引用旧 PVC。
   - 本环境没有 PVC 快照；回滚依赖“旧 PVC 未被删除、未被清空、未被重绑定”这一前提。
   - 如果曾经误清空或删除旧 etcd PVC，不能按本文档保证完整回滚，需要转入事故恢复流程。
   - API/worker 的 `MILVUS_HOST` 必须重新指向旧 Milvus service。
5. 如数据库 URL 字段已更新，执行以下二选一：
   - 恢复阶段四的 Postgres dump。
   - 执行反向 URL rewrite。
6. 如果阶段 14 后产生了新上传对象，业务需要决定：
   - 接受回滚期间新写入丢弃；
   - 或将 `rag-kb/openrag/<workspace_slug>/...` 中的新对象反向同步回旧 workspace bucket，并补齐数据库记录。
7. 启动旧 Milvus、旧 MinIO、旧 API/worker。
8. 按阶段一基线重新验证。

反向 URL rewrite 示例。执行前先把 `<old-base>` 替换为旧对象存储 URL 前缀，例如 `http://<old-minio>/<workspace_slug>/` 对应的可替换前缀；不同 workspace 如果旧 base 不同，应按 workspace 分批生成 SQL，不要盲目全局替换。

```sql
begin;

update files
set
  l0_path = case
    when l0_path like 'http://172.16.31.63:9000/rag-kb/openrag/%'
    then replace(l0_path, 'http://172.16.31.63:9000/rag-kb/openrag/', '<old-base>/')
    else l0_path
  end,
  l1_path = case
    when l1_path like 'http://172.16.31.63:9000/rag-kb/openrag/%'
    then replace(l1_path, 'http://172.16.31.63:9000/rag-kb/openrag/', '<old-base>/')
    else l1_path
  end,
  l2_path = case
    when l2_path like 'http://172.16.31.63:9000/rag-kb/openrag/%'
    then replace(l2_path, 'http://172.16.31.63:9000/rag-kb/openrag/', '<old-base>/')
    else l2_path
  end
where l0_path like 'http://172.16.31.63:9000/rag-kb/openrag/%'
   or l1_path like 'http://172.16.31.63:9000/rag-kb/openrag/%'
   or l2_path like 'http://172.16.31.63:9000/rag-kb/openrag/%';

update document_chunks
set object_url = replace(
  object_url,
  'http://172.16.31.63:9000/rag-kb/openrag/',
  '<old-base>/'
)
where object_url like 'http://172.16.31.63:9000/rag-kb/openrag/%';

-- 抽样检查后再 commit。
-- rollback;
commit;
```

回滚验收：

- 老文件预览成功。
- 老搜索结果恢复。
- worker 能处理新任务。
- API/worker 镜像 tag 与阶段 7.0 记录的旧 tag 一致。
- API/worker 的 `STORAGE_ENDPOINT`、`STORAGE_BUCKET`、`STORAGE_PREFIX`、`MILVUS_HOST` 与旧环境一致。
- 旧 MinIO 对象数量与阶段一基线一致，或差异已有业务说明。

## 16. 清理

观察期通过后再执行。清理前必须确认阶段 14 切换后已经稳定运行，且回滚窗口已结束。

1. 下线旧 `milvus-minio`。
2. 归档或删除旧 MinIO PVC；删除前必须确认旧 MinIO 对象级备份、Postgres dump、Milvus backup 和生产验收记录均已归档。
3. 归档或删除旧 Milvus/etcd PVC；本环境没有 PVC 快照，不存在“删除快照”步骤。
4. 删除 `rag-kb/milvus-backups/<timestamp>`，前提是已有离线归档或确认不再需要。
5. 轮换迁移期间使用过的临时凭据。
6. 归档阶段 7.0 的旧部署 YAML、旧镜像 tag、Postgres dump、MinIO inventory、Milvus backup 记录。
7. 保留第 5 阶段对应的 Git commit、镜像 tag 和 Kubernetes YAML 版本，作为最终生产版本记录。

清理禁忌：

- 不要在观察期内删除旧 API/worker 镜像 tag。
- 不要在观察期内删除旧 Deployment/Secret/ConfigMap 备份。
- 不要在确认 Milvus restore 可重复前删除 `rag-kb/milvus-backups/<timestamp>`。
- 不要在确认业务对象完整前删除旧 workspace bucket 或旧 MinIO PVC。

## 17. 参考资料

- Milvus MinIO/rootPath 配置：https://milvus.io/docs/v2.4.x/configure_minio.md
- Milvus Backup 概览：https://milvus.io/docs/v2.4.x/milvus_backup_overview.md
- Milvus 跨对象存储迁移：https://milvus.io/docs/v2.5.x/multi-storage-backup-and-restore.md
- Milvus Docker 配置挂载：https://milvus.io/docs/v2.4.x/configure-docker.md
- MinIO mc mirror：https://docs.min.io/enterprise/aistor-object-store/reference/cli/mc-mirror/

## 18. 后台进度记录

### 2026-05-23 - 前端 md 上传基本链路验证

- 用户已在前端成功上传 md 文件，说明 Web -> API -> 新 MinIO 写入链路已通过基本验证。
- 后续仍需验证该 md 文件处理任务是否 completed、检索是否可用，以及管理员页面 / Service Token 页面是否可访问。

### 2026-05-23 - 无图片 PDF 上传与处理链路验证

- 用户上传了一个无图片 PDF，经校验后相关任务全部 `success`；这验证了非 OCR PDF 的页面上传、API、MinIO、新 worker 处理、任务完成链路。
- 仍需单独处理/确认扫描件或含图 OCR PDF，因为此前剩余失败集中在 `OCR local model init failed and OPENRAG_DISABLE_HF_DOWNLOAD is enabled`。

### 2026-05-23 - 后台问题记录：前端检索 connectionerror

- 现象：前端检索时报 `connectionerror`。
- API 日志定位为 `openai.APIConnectionError` / `httpcore.ConnectError [Errno 101] Network is unreachable`。
- API Pod 环境为 `OPENAI_BASE_URL=https://api.openai.com/v1`，且 `OPENAI_API_KEY` 已设置；因此检索时调用公网 OpenAI embedding 失败。
- 结论：这不是 MinIO/Ingress 问题。
- 后续需选择内网可达 embedding 服务，或临时关闭 `OPENAI_API_KEY` 使用 mock embedding；本次仅做文档记录，不改代码或 k8s yaml。

### 2026-05-23 - 后台风险记录：1024 维 embedding 切换

- 用户在排查前端检索 `connectionerror` 后，已将 `OPENAI_BASE_URL` / embedding 服务切换到一个 1024 维 embedding 模型。
- 风险：现有 Milvus `openrag_chunks` collection 及层级向量大概率是 1536 维；如果 API/worker 以 1024 维 embedding 配置启动，可能导致向量维度不匹配、检索失败。
- 更高风险：根据当前代码逻辑，若检测到 collection dim 与 `embedding_engine.dimension` 不一致，可能 drop/recreate collection，存在原有向量索引丢失风险。
- 对话模型不一定必须和 embedding 模型一起更换；只有 L1 LLM navigation 或问答生成使用的 LLM 需要配置为内网可达。
