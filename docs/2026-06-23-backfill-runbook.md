# 回填目录行 执行手册（backfill runbook）

为**存量文档**（方案二上线前已上传、缺少目录行的文件）追溯补建目录行。
脚本：`openrag/scripts/backfill_directory_rows.py`。

---

## 0. 这个脚本要点

- **只依赖已安装的 `openrag` 包 + Python 标准库**，不联网、不装新依赖 → 可直接在已部署容器里跑（内网友好）。
- **数据库自动复用应用配置**（`POSTGRES_*` 环境变量 / `docker/.env` / `openrag/.env`），与 api / worker 同库。**在容器内运行时零额外配置**。
- **默认 dry-run**（只预览不写库）；必须显式加 `--apply` 才写入。
- **幂等**：可反复运行，已存在目录不会重复创建。

参数：

| 参数 | 含义 |
|---|---|
| `--workspace-id N` | 目标空间 id；可重复多次处理多个空间 |
| `--all-workspaces` | 处理所有空间（与 `--workspace-id` 互斥） |
| `--apply` | 实际写库；**不加则只预览** |

> 工作区 id 在 Web「文件」页 URL 或管理后台可见；不确定时先用 `--all-workspaces` 的**预览**看每个空间的统计。

---

## 1. 执行前准备（两种环境都建议）

备份 `files` 表（回填只新增 `is_directory=true` 行，影响面小，但生产务必先备份）：

```bash
# 在数据库容器所在主机执行
docker exec openrag-postgres-prod \
  pg_dump -U openrag -d openrag -t files --data-only > files_backup.sql
```

> 用户 / 库名以你的 `POSTGRES_USER` / `POSTGRES_DATABASE` 为准（默认 `openrag` / `openrag`）。
> 回填可在应用**在线运行时**执行（`ensure_directory_path` 并发安全），但仍建议选低峰期。

---

## 2. 场景 A —— 内网 / 生产（不能联网）★ 推荐

思路：脚本**拷进**正在运行的 api 容器（已装好 `openrag` 且已配好 DB），用容器内的 Python 直接跑。**全程无需联网、无需重建镜像**。

```bash
# 步骤 1：把脚本拷进运行中的 api 容器（无网络、无需 pip）
docker cp openrag/scripts/backfill_directory_rows.py \
  openrag-api-prod:/tmp/backfill_directory_rows.py

# 步骤 2：预览单个空间（dry-run，不写库）
docker exec -it openrag-api-prod \
  python /tmp/backfill_directory_rows.py --workspace-id 7

# 步骤 3：确认输出无误后，实际回填该空间
docker exec -it openrag-api-prod \
  python /tmp/backfill_directory_rows.py --workspace-id 7 --apply
```

处理**所有空间**：

```bash
# 先全局预览
docker exec -it openrag-api-prod \
  python /tmp/backfill_directory_rows.py --all-workspaces
# 确认后写库
docker exec -it openrag-api-prod \
  python /tmp/backfill_directory_rows.py --all-workspaces --apply
```

> - 容器名以 `docker ps` 实际为准（生产默认 `openrag-api-prod`；亦可用 `openrag-task-worker`，二者都装了 `openrag` 并连同一库）。
> - 若内网无 `docker cp` 权限：可改为把脚本内容粘贴进容器（`docker exec -i ... tee /tmp/backfill_directory_rows.py < backfill_directory_rows.py`），或将仓库 `openrag/scripts/` 目录挂载进容器后执行。

### Kubernetes 变体

```bash
kubectl cp openrag/scripts/backfill_directory_rows.py \
  <namespace>/<api-pod>:/tmp/backfill_directory_rows.py
kubectl exec -it <api-pod> -n <namespace> -- \
  python /tmp/backfill_directory_rows.py --workspace-id 7          # 预览
kubectl exec -it <api-pod> -n <namespace> -- \
  python /tmp/backfill_directory_rows.py --workspace-id 7 --apply  # 写库
```

---

## 3. 场景 B —— 外网 / 开发 · 测试（可联网）

在仓库里、用已安装 `openrag` 的虚拟环境直接运行。

```bash
cd openrag

# （首次）确保 openrag 已安装到当前环境；联网环境可：
#   pip install -e .         或    uv sync

# 确保 DB 指向目标库：经 openrag/.env 或 docker/.env 的 POSTGRES_* 配置
#   POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DATABASE
# 也可临时用环境变量覆盖，例如：
#   export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 \
#          POSTGRES_USER=openrag POSTGRES_PASSWORD=openrag_pass POSTGRES_DATABASE=openrag

python scripts/backfill_directory_rows.py --workspace-id 7           # 预览
python scripts/backfill_directory_rows.py --workspace-id 7 --apply   # 写库
```

> Windows PowerShell 用 `$env:POSTGRES_HOST="..."` 设置变量。
> 与生产同库时务必确认 `POSTGRES_*` 指向正确的库，避免误连。

---

## 4. 输出怎么读

```
== 回填目录行 [DRY-RUN（仅预览）] ==
[ws 7 / feishu-devops-wiki] 文件 42 个, 已有目录 0 个, 缺失目录 9 个
    + /feishu-devops-wiki
    + /feishu-devops-wiki/[WIP]研发
    + /feishu-devops-wiki/[WIP]研发/私有镜像仓库（Harbor）
    ...
---- 汇总 ----
空间 1 个, 文件 42 个, 缺失目录 9 个, 冲突 0 个, 已创建 0 个, 跳过 0 个
提示：以上为预览。确认无误后加 --apply 实际写库。
```

- `缺失目录`：将要创建的目录数（dry-run 下 `已创建=0`）。
- `--apply` 后会显示 `实际创建目录 N 个`。
- `冲突`：某个应为目录的路径已被**同名文件**占用（如同时存在文件 `/a` 与文件 `/a/b.md`）。这类会被**跳过并打印**，需人工确认（重命名其一）后再处理。

---

## 5. 验证

- **Web UI**：进入该空间「文件」页，左侧「项目文件目录」应出现对应目录并可逐级展开。
- **SQL**：
  ```sql
  SELECT count(*) FROM files WHERE workspace_id = 7 AND is_directory = true;
  ```
- **服务令牌**：`GET /service/workspaces/{name}/tree?path_prefix=/feishu-devops-wiki` 应返回节点而非 404。

---

## 6. 幂等与回滚

- **幂等**：再次 `--apply` 将显示 `缺失目录 0 / 已创建 0`，安全。
- **回滚**：最干净的方式是从第 1 步的 `files_backup.sql` 恢复。脚本创建的都是 `size=0, is_directory=true` 的目录行，保留通常无副作用。

---

## 7. 与代码上线的关系

- 本回填只针对**存量**数据。
- 新上传由**方案二**（`ingest_new_file` 内 `ensure_directory_path`）自动建目录行，无需再回填。
- 如需让脚本常驻镜像，可在 `docker/Dockerfile.api` / `Dockerfile.worker` 增加
  `COPY openrag/scripts/backfill_directory_rows.py ./scripts/`（非必需；`docker cp` 已足够应急）。
