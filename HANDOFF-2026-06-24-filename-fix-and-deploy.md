# 交接文档 — 上传文件名长度修复 + v1.1.3 外网部署

日期：2026-06-24 · 仓库：`e:\project\OpenRag` · 本次使用的 git 身份：`Deng-Lee <nanjingxilu_super@163.com>`（本会话已设为仓库级 git config）

## 一句话总结
用户反馈的"File name too long"上传报错，已定位根因、修复、合并到 `main`，并把新构建（**v1.1.3**）部署到了外网 Docker 服务器。部署**成功**（应用健康、ES 修复已生效）。但在部署后验收时发现一个**历史遗留隐患**：`openrag-api` 镜像里**没有打包 Alembic**，导致数据库迁移无法执行，且数据库没有 `alembic_version` 基线。这对 v1.1.3 **没有影响**（本次无迁移），但对**以后任何带数据库迁移的版本**是地雷。该修复**尚未完成**，是当前最主要的未决事项。

## 已交付（均已提交，勿重复实现）
分支 `fix/upload-filename-too-long`（从 `origin/main` 切出），已 fast-forward 合并进本地 `main`。**`main` 尚未推送到 `origin`。**

`main` 上的提交（从新到旧）：
- `a34b2e0` feat(web)：单文件上传时，文件名过长的本地化 toast
- `2a26622` update .env —— **用户本人**的 ES 地址修复（`ELASTICSEARCH__HOSTS` localhost→elasticsearch）；非助手提交
- `42313f4` fix(upload)：文件名过长时返回清晰的 400

根因与设计理由见提交信息和代码注释，此处不重复。关键位置：
- 根因：`openrag/src/openrag/worker/task_worker.py:295-298` 把临时文件写成 `doc_{id}_{basename}`；操作系统对单个路径分量限制 255 **字节**（ENAMETOOLONG）。中文（UTF-8 3 字节/字）文件名约 80 字就会触发。
- 后端校验：`MAX_FILENAME_BYTES = 200` + `openrag/src/openrag/services/file_ingest.py` 里的校验（web `/files/upload` 和 service-token 上传共用的收口点）。
- 前端镜像校验：`web/src/utils/folderUpload.ts`（`filenameBytes`、`isFilenameTooLongError`、precheck 跳过原因 `name_too_long`）；toast 在 `web/src/components/FileUpload.tsx`；i18n 键 `files.upload.skip_name_too_long` / `files.upload.name_too_long` 位于 `web/src/i18n/locales/{en,zh}.json`。
- 测试：`openrag/tests/test_upload_filename_length.py`、`web/src/utils/folderUpload.test.ts` 新增用例。本地全绿（pytest、vitest 28 项、`tsc --noEmit`）。

## 部署状态（v1.1.3）—— 已成功
- 构建：`.\scripts\build-openrag-compose-release.ps1 -Version 1.1.3`（不加 `-IncludeThirdPartyImages` → 复用服务器已有的第三方镜像）。制品在 `artifacts/`；`openrag-app-images-1.1.3.tar`（3.93 GB）含 3 个应用镜像（已通过 tar manifest 核实：openrag-api/web/task-worker）。`manifest.json` 的 git_commit = `a34b2e0`。
- 服务器（外网，经 MobaXterm）：主机 `guozhi@guozhi-acloud`，`SERVER_HOME=/home/guozhi/Documents/OpenRag`，`API_PORT=18001`，`WEB_PORT=80`。
- 分工：助手本地构建；**所有服务器端命令由用户执行**。
- `.env` 改动在服务器上**只改一行**（`sed` 改 `ELASTICSEARCH__HOSTS`），**没有整体覆盖 `openrag.env`**——避免冲掉可能与本地 `docker/.env` 不一致的生产密钥。
- 验收全绿：`current → releases/openrag-1.1.3`；api 和 task-worker 的 `ELASTICSEARCH__HOSTS` 都是 `http://elasticsearch:9200`；`/health` + `/api/health` 健康且 `database: connected`；3 个应用镜像 `built=2026-06-24`；`up 8/8` 容器 running/healthy。

部署指南（勿重复内容）：`docs/外网DockerCompose部署指南.md`。

## 未决事项 / 风险（下一会话从这里开始）
1. **🔴 api 镜像缺 Alembic（最大事项，未修复）。**
   - `docker/Dockerfile.api:54-56` 复制了 `src/`、`run_api.py`、`setup.py`，但**缺少**指南 §5 要求的两行：
     ```dockerfile
     COPY openrag/alembic.ini .
     COPY openrag/alembic/ ./alembic/
     ```
   - 已通过检查构建出的镜像确认：`/app/alembic.ini` 和 `/app/alembic` 都不存在（WORKDIR=`/app`）。所以执行 `alembic upgrade head` 会报 "No 'script_location' key found"。
   - 数据库**没有 `alembic_version` 表**——表结构当初是用 `Base.metadata.create_all()` 兜底建的（指南 §19 兜底场景），从未迁移过。
   - 影响：对 v1.1.3 无影响（无模型改动）；但以后任何带迁移的版本将无法迁移，且 `create_all()` 不会给已有表加列 → 可能出现 `column ... does not exist` 运行时错误。
   - 建议的三步修复（助手已建议，待用户拍板）：(a) 加上这两行 COPY 并提交（对仓库零风险）；(b) 重建 api 镜像并重新部署；(c) **`alembic stamp <与当前 schema 对应的 revision>`** 初始化基线——**不是** `upgrade head`（那会去 CREATE 已存在的表）。第 (c) 步会动生产数据库 → 须先核对 schema↔revision 的对应关系。

2. **🟡 当前 schema 完整性未核实。** 建议的下一步是一条 1 分钟的服务器检查，用户**尚未执行**：
   ```bash
   docker exec -i openrag-postgres-prod psql -U openrag -d openrag <<'SQL'
   SELECT table_name, column_name FROM information_schema.columns
   WHERE (table_name='files' AND column_name='document_type')
      OR (table_name='document_chunks' AND column_name IN ('page_num_int','position_int','top_int'))
   ORDER BY 1,2;
   SQL
   ```
   4 行都返回 ⇒ 当前 schema 对 v1.1.3 是安全的。这是 /handoff 被触发时的下一个待办。

3. **🟡 ES BM25 补索引。** 如果服务器之前一直跑 `localhost:9200`（ES 实际失效），则之前上传的文档不在 BM25 关键词索引里。现在只有新上传的文档会入索引，旧文档需要重新处理。向量检索（Milvus）不受影响。

4. **🟢 服务器缺 `migrate-openrag` 辅助脚本。** 服务器上的 `deploy-openrag-compose-release.sh` 是旧版本，只生成 `dc-openrag`，不生成 `migrate-openrag`。已提供一条 `cat >` 命令单独创建它（见对话），用户尚未执行。等效的手动迁移命令：`"$SERVER_HOME/shared/dc-openrag" run --rm --no-deps api python -m alembic upgrade head`（目前因第 1 项缺 Alembic 会失败）。

## 待用户决策
- 现在跑 §20 schema 检查吗？（推荐）
- 应用 Dockerfile.api 的两行修复并提交吗？（助手已提议；对代码零风险）
- 把 `main` 推到 `origin` 吗？（已本地合并，未推送）
- 安排 api 镜像重建/重新部署 + alembic 基线 `stamp`（较敏感，动生产库）？
- 清理：`artifacts/openrag-web-1.1.3.tar`（6-23 的旧文件）与本次构建无关，可删除。

## 给下一个 agent 的建议技能
- **superpowers:systematic-debugging** —— 用于 Alembic 基线工作（第 1c 项）：在 `stamp` 前判断 create_all 建出的 schema 对应哪个 revision。
- **superpowers:verification-before-completion** —— 在声称迁移管线"已彻底修好"之前，要有证据（跑 schema 检查、重建后跑 alembic），而非断言。
- **andrej-karpathy-skills:karpathy-guidelines** —— Dockerfile.api 的改动应保持外科手术式（只加那两行 COPY）。
- **/run** 或 **/verify** —— 在运行中的应用上确认新的文件名校验功能（上传一个 >200 字节的文件名，应弹出本地化 toast）。

## 备注
- 本文档不含任何密钥。会话期间从未打印真实 `.env` 值（POSTGRES/MINIO/OPENAI 等），且不得提交（源码包已排除 `docker/.env`）。
- 按用户既定偏好，本交接文档写入项目路径（非 OS 临时目录）。
