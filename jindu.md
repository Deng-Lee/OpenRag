# OpenRag 部署计划执行进度

创建时间：2026-05-20 17:07:46 +08:00

本文件记录 `bushu.md` 的逐步执行进度。每个步骤必须在执行、总结并获得人工确认后，才能标记为完成。

## 进度日志

### 2026-05-20 17:09:16 +08:00 - 已完成：1. 目标部署拓扑

- 来源步骤：`bushu.md` 第 1 节“目标部署拓扑”。
- 执行内容：确认本次部署目标为外部 MinIO 单 bucket 模式，OpenRag API/Worker、Milvus、Elasticsearch、Postgres、Web/Ingress 的目标拓扑已明确。
- 对象存储约定：OpenRag 业务对象使用 `rag-kb/openrag`，Milvus 在线向量对象使用 `rag-kb/milvus`，Milvus backup 中转数据使用 `rag-kb/milvus-backups/<timestamp>`。
- 重要结论：正式部署路径不再依赖集群内 `milvus-minio`。
- 文件变更：创建并初始化 `jindu.md`，随后记录本步骤完成状态。
- 验证结果：已用 UTF-8 正确读取 `bushu.md`，确认第 1 节内容与上述拓扑和对象存储约定一致。
- 人工确认：用户于 2026-05-20 17:09 左右回复“已完成”。

### 2026-05-20 17:20:47 +08:00 - 已完成：2. 本地清单调整策略

- 来源步骤：`bushu.md` 第 2 节“本地清单调整策略”。
- 执行内容：完成外部 MinIO 单 bucket 模式下的本地 Kubernetes 清单检查与配置调整。
- 文件变更：
  - `k8s/01-secret.example.yaml`：外部 MinIO 凭据示例调整为 `raguser` / `CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY`，并补充 `MILVUS_SECRET_KEY`。
  - `k8s/01-secret.yaml`：创建本地占位 Secret 文件，真实密钥仍需人工替换，不应提交到 Git。
  - `k8s/07-milvus.yaml`：Milvus 指向 `172.16.31.63:9000`，并从 `MILVUS_SECRET_KEY` 读取外部 MinIO secret。
  - `k8s/07-milvus-config.yaml`：Milvus `bucketName/rootPath` 保持 `rag-kb/milvus`，secret 占位符统一为 `CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY`。
  - `k8s/kustomization.yaml`：正式资源列表移除 `06-milvus-minio.yaml`，保留外部 MinIO 所需 ConfigMap。
  - `k8s/overlays/private-registry/kustomization.yaml`：移除内置 MinIO，补入 `07-milvus-config.yaml` 和 `15-configmap-openrag-service-conf.yaml`。
- 验证结果：YAML 解析通过；`kubectl kustomize k8s` 可渲染；渲染结果包含 `bucketName: rag-kb`、`rootPath: milvus`、`MINIO_ADDRESS`、`STORAGE_ENDPOINT`，未匹配到 `milvus-minio` / `06-milvus-minio`。
- 注意事项：`private-registry` overlay 仍是引用父目录文件的布局，需要 `--load-restrictor=LoadRestrictionsNone` 才能渲染；如后续直接使用 `kubectl apply -k k8s/overlays/private-registry`，建议整理 overlay/base 目录结构。
- 人工确认：用户于 2026-05-20 17:20 左右回复“进行下一步”，视为确认第 2 步已完成并允许继续。

### 2026-05-20 17:24:27 +08:00 - 已完成：3. 构建机预检查

- 来源步骤：`bushu.md` 第 3 节“构建机预检查”。
- 执行内容：检查构建机 Docker Engine、项目目录结构和 Worker 离线模型文件。
- 文件变更：无。
- 验证结果：
  - `docker info` 执行成功，Docker Desktop Linux Engine 正常运行，Server Version 为 `29.4.2`，当前 Docker context 为 `desktop-linux`。
  - 项目目录检查通过：`docker`、`openrag`、`web`、`k8s` 均存在。
  - Worker 离线模型文件检查通过：`det.onnx`、`rec.onnx`、`ocr.res`、`layout.onnx`、`tsr.onnx`、`updown_concat_xgb.model` 均存在于 `openrag/rag/res/deepdoc`。
- 注意事项：原计划中的 `Get-ChildItem openrag/rag/res/deepdoc | Select-String ...` 会读取大模型文件内容并超时，实际验证改用文件名匹配，验证目标等价且不会读取大文件内容。
- 人工确认：用户于 2026-05-20 17:24 左右回复“确认完成”。

### 2026-05-20 19:21:08 +08:00 - 已完成：4. 构建业务镜像

- 来源步骤：`bushu.md` 第 4 节“构建业务镜像”。
- 执行内容：构建并验收 OpenRag API、Task Worker、Web 三个业务镜像。
- 执行版本：本轮实际使用 `TAG=1.1.0`，替代 `bushu.md` 示例中的 `1.0.9`。
- 构建产物：
  - `openrag/api:1.1.0`
  - `openrag/task-worker:1.1.0`
  - `openrag/web:1.1.0`
- 文件变更：无。
- 验证结果：
  - `docker image inspect openrag/api:1.1.0 openrag/task-worker:1.1.0 openrag/web:1.1.0` 执行成功，三张目标镜像均存在。
  - `docker images | Select-String "openrag"` 显示三张 `1.1.0` 镜像：`openrag/api`、`openrag/task-worker`、`openrag/web`。
  - Web 镜像构建日志显示 `npm run build` 成功，Vite build completed。
- 注意事项：构建过程中 Docker Desktop 曾在 Worker 构建超时后返回 500，用户重启 Docker 后恢复；恢复后完成 Web 构建与三镜像验收。
- 人工确认：用户于 2026-05-20 19:21 左右回复“确认已完成”。

### 2026-05-20 19:25:44 +08:00 - 已完成：5. 拉取第三方镜像

- 来源步骤：`bushu.md` 第 5 节“拉取第三方镜像”。
- 执行内容：检查并拉取外部 MinIO 模式所需第三方 Docker 镜像。
- 文件变更：无。
- 镜像结果：
  - `postgres:16-alpine`：已存在。
  - `quay.io/coreos/etcd:v3.5.5`：已存在。
  - `milvusdb/milvus:v2.4.17`：已存在。
  - `docker.elastic.co/elasticsearch/elasticsearch:8.12.2`：已存在。
  - `busybox:1.36`：原本缺失，已拉取成功。
- 验证结果：
  - `docker image inspect` 对五个目标镜像均执行成功。
  - `docker images | Select-String "postgres|etcd|milvus|elasticsearch|busybox"` 确认五个目标镜像均在本机镜像列表中。
- 注意事项：按 `bushu.md` 外部 MinIO 模式约定，正式部署不需要 `minio/minio`，因此未拉取该镜像。
- 人工确认：用户于 2026-05-20 19:25 左右回复“确认第5步已完成”。

### 2026-05-20 19:30:04 +08:00 - 已完成：6. 生成离线镜像包

- 来源步骤：`bushu.md` 第 6 节“生成离线镜像包”。
- 执行内容：基于本轮实际部署版本 `TAG=1.1.0` 生成外部 MinIO 模式离线镜像包和 SHA256 校验文件。
- 文件变更：
  - 新增/写入 `openrag-offline-1.1.0-external-minio-images.tar`。
  - 新增/写入 `openrag-offline-1.1.0-external-minio-images.tar.sha256`。
- 产物信息：
  - `openrag-offline-1.1.0-external-minio-images.tar`：`5,379,404,288 bytes`，约 `5.01 GiB`。
  - `openrag-offline-1.1.0-external-minio-images.tar.sha256`：`56dcee29310bc45c380cc4c149cd50cffd7604709270df5704eb20afd660a80a  openrag-offline-1.1.0-external-minio-images.tar`。
- 验证结果：
  - `docker image inspect` 确认 8 个必需镜像均存在。
  - `docker save -o openrag-offline-1.1.0-external-minio-images.tar ...` 执行完成。
  - `Get-FileHash -Algorithm SHA256` 生成校验值，并复核 `.sha256` 中记录的 hash 与 tar 实际 hash 一致，`ShaMatches=True`。
- 注意事项：本轮只生成部署制品文件，未修改代码、测试、构建脚本或运行逻辑。
- 人工确认：用户于 2026-05-20 19:30 左右回复“确认第六步已完成”。

### 2026-05-23 10:25:00 +08:00 - 记录：阶段十一 apply 前门禁 / server-side dry-run（未正式切换）

- 记录类型：这是阶段十一正式生产 apply 前的门禁和 dry-run 记录，不表示 API、worker、web 已完成生产切换，也不把 `bushu.md` 后续部署步骤标记为完成。
- 执行环境：用户已在内网 `master1 / 172.16.22.13` 的 `/root/lisiqi/openrag-migration/data` 目录创建并使用渲染目录 `k8s-rendered-stage11-20260523101628`。
- 渲染目录状态：实际发布文件已完成阶段十一 apply 前检查，`09-api.yaml`、`10-task-worker.yaml`、`11-web.yaml` 的镜像应分别指向 `openrag/api:1.1.0`、`openrag/task-worker:1.1.0`、`openrag/web:1.1.0`，且 `imagePullPolicy` 为 `IfNotPresent`。
- 检查说明：全目录 `grep` 曾出现两类可解释命中，分别是 `11-web.yaml.bak` 中仍有 `imagePullPolicy: Always`，以及 `01-secret.example.yaml` 中仍有 `CHANGE_ME_EXTERNAL_MINIO_SECRET_KEY`。这两个文件不是本次发布目标文件，不阻塞；随后限定到实际发布文件的检查已通过。
- server-side dry-run 结果：实际发布文件 dry-run 已通过，输出包括 `secret/openrag-secrets configured (server dry run)`、`configmap/milvus-config configured (server dry run)`、`configmap/openrag-service-conf created (server dry run)`、`persistentvolumeclaim/openrag-api-uploads unchanged (server dry run)`、`service/api unchanged (server dry run)`、`deployment.apps/openrag-api configured (server dry run)`、`deployment.apps/openrag-task-worker configured (server dry run)`、`service/web unchanged (server dry run)`、`deployment.apps/openrag-web configured (server dry run)`。
- 尚未执行：正式生产 `kubectl apply` 尚未执行，API/worker/web 尚未据此记录为已切换；后续不能把本条记录误读为阶段十一完成。
- 敏感制品：渲染目录包含已替换真实外部 MinIO Secret 的文件，本文不记录真实 Secret；该渲染目录以及后续切换前快照目录都应按敏感制品处理，不应提交到 Git 或外传。
- 下一步建议：先保存切换前快照，再 apply Secret/ConfigMap，再只滚动 API 并验证，再滚动 Web，最后恢复或滚动 worker。

### 2026-05-23 10:36:40 +08:00 - 异常观察：阶段十一 worker 切换/恢复后 S3 NoSuchKey（未验收通过）

- 记录类型：这是阶段十一切换后的异常观察，不等同于阶段十一切换验收通过，也不表示 worker 后续批量任务可以继续放量执行。
- 已完成验证：用户已完成 worker 切换/恢复后的验证；worker 环境变量显示 `STORAGE_BUCKET=rag-kb`、`STORAGE_PREFIX=openrag`、`STORAGE_ENDPOINT=http://172.16.31.63:9000`、`MILVUS_HOST=milvus`，镜像为 `openrag/task-worker:1.1.0`。
- 异常现象：worker 日志出现 S3 `NoSuchKey`，错误信息包含 `Object does not exist`、`bucket_name=rag-kb`，`object_name` 形如 `openrag/law/regulations/...`，`resource` 形如 `/rag-kb/openrag/law/...`。
- 任务状态：示例失败任务包括 `Task 1204`、`Task 1625`；随后仍观察到 `Task 786`、`Task 1198` 处于 `executing`。
- 风险边界：需要暂停或谨慎处理 worker 后续批量任务，先定位缺失对象根因，避免继续产生大批量失败任务或扩大数据不一致面。
- 下一步建议：检查 `new-minio/rag-kb/openrag/law` 下实际对象路径；对失败日志中的具体 `object_name` 执行 `mcli stat new-minio/rag-kb/<object_name>`；与 `old-minio/law` 对比，判断是 mirror 漏对象、路径编码/空格差异，还是数据库引用与对象迁移结果不一致。

### 2026-05-23 10:43:25 +08:00 - 排查进展：NoSuchKey 失败对象三方对照（未确认源/备份）

- 记录类型：这是 NoSuchKey 后续排查进展记录，不阻塞主流程；但在根因未闭环前，不能继续把阶段十一验收记为通过。
- 失败对象：`OBJ` 形如 `openrag/law/regulations/sse/rules/本所业务规则/repeal/已废止规则文本/上海证券交易所科创板上市公司证券发行上市审核规则.doc`；其中旧 MinIO 对照用的 `REL` 为去掉 `openrag/law/` 后的相对路径。
- 新目标 MinIO 对照：用户按三方对照检查失败对象时，`mcli stat new-minio/rag-kb/$OBJ` 返回 `Object does not exist`，确认新 MinIO 目标路径缺对象。
- 旧在线 MinIO 对照：检查 `old-minio/law/$REL` 时出现 `http://127.0.0.1:19000/law/?location= dial tcp 127.0.0.1:19000 connect refused`，说明 `old-minio` alias 依赖的 `kubectl port-forward` / 本地 `19000` 端口当前未运行，或旧 MinIO 服务不可达；因此尚不能判定旧在线 MinIO 是否有该对象。命令输出还出现 `old-minio stat object does not exist`，但必须在恢复 old-minio 连接后重试确认，不能据此直接判定源对象不存在。
- 本地备份对照：`find` 本地备份命令未显示匹配输出（以截图为准），尚未确认备份中是否存在该对象。
- 当前结论：目前只确认新 MinIO 目标路径缺对象，尚未确认旧在线 MinIO 或本地备份是否存在源对象；worker 应继续保持缩容/冻结，避免继续放大失败任务和数据不一致面。
- 下一步建议：先恢复旧 MinIO 的 `kubectl port-forward` / 本地 `19000` 连接并重试 `old-minio/law/$REL` 精确 `stat`；或直接定位本地备份目录后对该文件名和相对路径做精确 `find` / `stat`。

### 2026-05-23 10:52:04 +08:00 - 排查进展：NoSuchKey 样本确认旧 MinIO 存在、目标 MinIO 缺失（未验收通过）

- 记录类型：这是 NoSuchKey 根因定位的后台补充记录，不阻塞主流程；但阶段十一验收仍未通过，不能恢复 worker 放量执行。
- worker 冻结状态：worker 已成功缩容到 `0`，当前 `openrag-task-worker` Pod 无资源；在补同步和复验完成前，worker 应继续保持冻结。
- 失败对象：`old-minio/law/regulations/sse/rules/本所业务规则/repeal/已废止规则文本/上海证券交易所科创板上市公司证券发行上市审核规则.doc`。
- 旧在线 MinIO 对照：旧 MinIO `port-forward` 恢复后，`mcli stat/find` 证明旧在线 MinIO `old-minio/law` 中存在该失败对象；对象大小 `79 KiB`，`Content-Type=application/msword`，`ETag=888896054f4c7fb7bf30238ec5e0ab86`。
- 新目标 MinIO 对照：此前 `new-minio/rag-kb/openrag/law/...` 同一路径缺失，`mcli stat new-minio/rag-kb/openrag/law/...` 返回 `Object does not exist`。
- 当前结论：至少对该样本而言，`NoSuchKey` 更像业务对象 mirror/补同步缺失，而不是数据库凭空引用。
- 下一步主流程：补同步 `old-minio/law` 到 `new-minio/rag-kb/openrag/law`，随后复验该失败对象；补同步和复验完成前，阶段十一验收不得记为通过，worker 保持冻结。

### 2026-05-23 10:57:03 +08:00 - 后台记录：law 补同步后 NoSuchKey 样本复验成功（仍未恢复 worker）

- 记录类型：这是 NoSuchKey 样本补同步后的后台补充记录，不阻塞主流程；但只证明至少该样本已修复，不能视为阶段十一整体验收通过。
- 补同步操作：用户执行 `mcli mirror --overwrite old-minio/law new-minio/rag-kb/openrag/law`，用于补齐 `law` workspace 业务对象到外部 MinIO 单桶目标路径。
- 复验结果：补同步后重新执行 `mcli stat new-minio/rag-kb/$OBJ` 已成功。
- 样本对象：`Name=上海证券交易所科创板上市公司证券发行上市审核规则.doc`，`Size=79 KiB`，`ETag=888896054f4c7fb7bf30238ec5e0ab86`，`Content-Type=application/msword`。
- 对照结论：该样本在新 MinIO 的 ETag 和大小与旧 MinIO 样本一致，说明至少这个 `NoSuchKey` 样本已由本次补同步修复。
- worker 状态要求：worker 仍应保持缩容/冻结，不能因单个样本复验成功就恢复放量。
- 下一步主流程：继续做 `old-minio/law` 与 `new-minio/rag-kb/openrag/law` 的对象数量对比；对 `law` 做必要抽样；再做 `old-minio/test` 与 `new-minio/rag-kb/openrag/test` 的对象数量对比，并在必要时补同步 `test`；上述对比、抽样和 `test` 补同步完成后，才考虑恢复 worker。

### 2026-05-23 10:57:44 +08:00 - 后台记录：law/test 业务对象补同步与数量对比完成（准备谨慎恢复 worker 1 副本）

- 记录类型：这是 NoSuchKey 后续对象补同步完成度的后台补充记录，不阻塞主流程；不记录任何 Secret，也不把阶段十一整体验收提前标记为通过。
- `law` 对象数量对比：用户完成 `law` 业务对象补同步和数量对比，`mcli ls --recursive old-minio/law | wc -l = 48994`，`mcli ls --recursive new-minio/rag-kb/openrag/law | wc -l = 48994`。
- `test` 补同步操作：用户执行 `mcli mirror --overwrite old-minio/test new-minio/rag-kb/openrag/test`，用于补齐 `test` workspace 业务对象到外部 MinIO 单桶目标路径。
- `test` 对象数量对比：`mcli ls --recursive old-minio/test | wc -l = 178`，`mcli ls --recursive new-minio/rag-kb/openrag/test | wc -l = 178`。
- 当前结论：`law` 和 `test` 业务对象数量已与阶段一旧 MinIO 基线一致；之前用于定位 NoSuchKey 的样本对象也已在新 MinIO 目标路径 `stat` 成功。
- 下一步主流程：谨慎恢复 worker `1` 副本并观察日志；如果仍出现 `NoSuchKey`，继续按失败日志中的具体 `object_name` 逐个执行 `mcli stat new-minio/rag-kb/<object_name>` 定位。

### 2026-05-23 11:00:44 +08:00 - 后台记录：worker 1 副本观察窗口未见异常（仍需业务验收）

- 记录类型：这是阶段十一恢复 worker 后的正向观察记录，不阻塞主流程；不把阶段十一最终验收标记为完成。
- 观察操作：用户恢复 `openrag-task-worker` 为 `1` 副本后，执行 `kubectl -n openrag logs -f deploy/openrag-task-worker` 观察日志。
- 用户反馈：观察日志无异常。
- 观察结论：补同步 `law`/`test` 后，worker `1` 副本观察窗口内未再看到 `NoSuchKey`、`S3 operation failed`、`Object does not exist`、`Traceback` 等异常。
- 验收边界：这说明阶段十一恢复 worker 具备正向观察结果，但仍不是阶段十一最终验收完成。
- 下一步建议：继续做业务功能验收，包括旧文件预览/下载/检索、新上传对象落点、任务堆积检查，以及 API/worker 当前镜像和环境变量检查；除非用户后续确认业务验收完成，否则不要把阶段十一最终验收标记为完成。

### 2026-05-23 11:03:40 +08:00 - 后台记录：worker 恢复后日志过滤与 tasks 状态检查（仍需追查 failure）

- 记录类型：这是阶段十一 worker 恢复后的任务状态检查后台记录，不阻塞主流程；不把阶段十一业务验收标记为完成。
- 日志过滤操作：用户执行 `kubectl -n openrag logs deploy/openrag-task-worker --tail=500 | grep -Ei 'NoSuchKey|S3 operation failed|Object does not exist|Traceback|Exception|ERROR' || true`。
- 日志过滤结果：输出只有 Kubernetes 的 `Defaulted container` 提示，未见 `NoSuchKey`、`S3 operation failed`、`Object does not exist`、`Traceback`、`Exception`、`ERROR` 命中。
- tasks 状态查询结果：`cancelled=1`、`failure=166`、`success=1413`。
- 当前判断：该结果说明 worker 当前日志观察无明显新增对象读取异常；但 `failure=166` 是否为历史遗留或刚才切换过程中新增，尚需按 `tasks` 时间字段和错误字段追查。
- 验收边界：阶段十一业务验收仍未最终完成。
- 下一步主流程：查询 `tasks` 表字段和最近失败任务详情。

### 2026-05-23 11:04:31 +08:00 - 后台记录：tasks/files 排查结果与 S3 missing failure 结论（仍未最终验收）

- 记录类型：这是阶段十一 worker 恢复后的 `tasks` / `files` 深入排查后台记录，不阻塞主流程；只记录当前事实和后续处置边界，不把阶段十一业务验收标记为完成。
- DB 查询时间：用户查询数据库时间为 `2026-05-23 03:04:31 UTC`。
- 最近 2 小时 tasks 状态统计：只有 `failure=161`。
- 最近失败任务详情：多个 `failure` 任务创建时间集中在 `2026-04-30 01:34/01:35` 左右；错误均为 `S3 operation failed` / `NoSuchKey` / `Object does not exist`，`resource` 指向 `/rag-kb/openrag/law/regulations/...`。
- S3 缺对象失败统计：`s3_missing_failures=161`。
- files 当前统计：`law file_rows=1573`、`completed_documents=1406`、`failed_documents=166`；`test file_rows=3`、`completed_documents=2`、`failed_documents=0`。
- 与阶段一基线对比：阶段一 `law failed_documents=116`，当前已增加到 `166`，说明切换/恢复 worker 期间至少有新增失败文件，或历史任务被重新标记为失败。
- 当前结论：对象补同步已完成，但数据库里的失败任务状态和文件失败状态不会自动恢复；阶段十一仍未最终验收通过。
- 下一步主流程：先确认 `NoSuchKey` 失败样本对象现已存在，再决定是否批量 retry/reset 这批 S3 missing failure 任务及对应文件状态。

### 2026-05-23 11:51:13 +08:00 - 后台记录：S3 failure recovery SQL 后 worker 重跑状态变化（暂停继续追查）

- 记录类型：这是 `migration_stage11_s3_failure_recovery_20260523` 相关恢复任务重跑后的后台状态记录，不阻塞主流程；不把阶段十一最终验收标记为完成。
- 已发生事实：S3 failure recovery SQL 已提交后，worker 被恢复运行并处理了一部分 `pending` 任务。
- 当前 tasks 状态：`cancelled=1`、`failure=39`、`pending=61`、`started=2`、`success=1477`。
- 对比基线：刚重置后的状态为 `cancelled=1`、`failure=5`、`pending=161`、`success=1413`。
- 状态变化：`success` 增加 `64`，`pending` 减少 `100`，但 `failure` 增加 `34`，且仍有 `2` 个任务处于 `started`。
- 当前判断：worker 确实推进了一部分重跑任务，但新增 `failure` 需要先确认错误类型，不能据此认定 S3 failure recovery 已闭环。
- 主流程决定：先暂停 worker，检查 `migration_stage11_s3_failure_recovery_20260523` 这批任务中当前失败的错误类型，确认是否仍为 S3 `NoSuchKey`，还是已经转为其他解析/业务错误。
- 验收边界：阶段十一仍不能标记为最终完成。

### 2026-05-23 12:02:59 +08:00 - 后台记录：S3 failure recovery 错误类型收敛为 OCR/解析模型环境问题（不阻塞主流程）

- 记录类型：这是 `migration_stage11_s3_failure_recovery_20260523` 批次当前失败类型收敛结论的后台记录，不阻塞主流程；不把阶段十一最终验收标记为完成。
- 批次当前状态：用户查询该批次当前状态，结果为 `failure=41`、`pending=33`、`started=2`、`success=85`。
- 错误类型统计：当前 `failure` 中 `error_type=other` 为 `41`，没有 `s3_missing` / `s3_operation` 命中。
- 失败样本错误内容：`OCR local model init failed and OPENRAG_DISABLE_HF_DOWNLOAD is enabled`。
- 收敛结论：对象补同步后，当前新增失败已从 MinIO/S3 `NoSuchKey` 问题转为 OCR/解析模型环境问题，不再属于外部 MinIO 迁移路径缺对象问题。
- 主流程建议：不继续用 MinIO 迁移流程处理 OCR 失败；先停止 worker，等待 `started` 任务收敛或按策略恢复为 `pending`，再做迁移验收，并单独记录 OCR 模型环境问题。
- 验收边界：阶段十一仍需完成业务验收，但 MinIO `NoSuchKey` 根因已收束。

### 2026-05-23 12:23:11 +08:00 - 后台记录：阶段十一恢复批次完成和问题收束结论（仍需业务冒烟）

- 记录类型：这是阶段十一恢复批次完成后的后台收束记录，不阻塞主流程；不把阶段十一最终验收标记为完成。
- 恢复批次观察：用户恢复 worker 后使用固定循环观察 recovery 批次，`active_recovery_tasks` 从 `35` 逐步下降到 `0`，观察窗口为 `2026-05-23 12:14:10 +08:00` 到 `2026-05-23 12:23:11 +08:00`。
- 批次最终状态：随后查询 `migration_stage11_s3_failure_recovery_20260523` 批次，最终结果为 `failure=61`、`success=100`。
- 失败类型统计：失败中 `ocr_model=59`、`other=2`，没有 `s3_missing` 或 `s3_operation`。
- worker 日志复核：最近 30 分钟 worker 日志 grep `NoSuchKey`、`S3 operation failed`、`Object does not exist` 无命中，仅有 Kubernetes `Defaulted container` 提示。
- 收束结论：MinIO/S3 `NoSuchKey` 缺对象问题已通过补同步和恢复批次处理收束；剩余失败主要为 `OCR local model init failed and OPENRAG_DISABLE_HF_DOWNLOAD is enabled`，属于 OCR/解析模型环境问题，不应继续按 MinIO 迁移问题处理。
- 验收边界：阶段十一还需完成业务冒烟验收后才能最终标记完成。

### 2026-05-23 - 后台记录：前端 md 上传基本链路已通过基本验证

- 用户已在前端成功上传 md 文件，说明 Web -> API -> 新 MinIO 写入链路已通过基本验证。
- 后续仍需验证该 md 文件处理任务是否 completed、检索是否可用，以及管理员页面 / Service Token 页面是否可访问。

### 2026-05-23 - 后台记录：无图片 PDF 上传与处理链路验证通过

- 用户上传了一个无图片 PDF，经校验后相关任务全部 `success`；这验证了非 OCR PDF 的页面上传、API、MinIO、新 worker 处理、任务完成链路。
- 仍需单独处理/确认扫描件或含图 OCR PDF，因为此前剩余失败集中在 `OCR local model init failed and OPENRAG_DISABLE_HF_DOWNLOAD is enabled`。

### 2026-05-23 - 后台问题记录：前端检索 connectionerror

- 现象：前端检索时报 `connectionerror`。
- API 日志定位为 `openai.APIConnectionError` / `httpcore.ConnectError [Errno 101] Network is unreachable`。
- API Pod 环境为 `OPENAI_BASE_URL=https://api.openai.com/v1`，且 `OPENAI_API_KEY` 已设置；因此检索时调用公网 OpenAI embedding 失败。
- 结论：这不是 MinIO/Ingress 问题。
- 后续需选择内网可达 embedding 服务，或临时关闭 `OPENAI_API_KEY` 使用 mock embedding；本次仅做文档记录，不改代码或 k8s yaml。
