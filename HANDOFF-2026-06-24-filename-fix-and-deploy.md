# 交接文档 — v1.1.3 外网部署 + Alembic 基线隐患（问题清单）

日期：2026-06-24 · 仓库：`e:\project\OpenRag`

## 一句话总结
新构建（**v1.1.3**）已部署到外网 Docker 服务器，部署成功（应用健康、ES 修复已生效）。但部署后验收发现一个历史遗留隐患：`openrag-api` 镜像里没有打包 Alembic，导致数据库迁移无法执行，且数据库没有 `alembic_version` 基线。对 v1.1.3 无影响（本次无迁移），但对以后任何带数据库迁移的版本是地雷。

## 现存问题
1. **🔴 api 镜像缺 Alembic。**
   - `docker/Dockerfile.api:54-56` 复制了 `src/`、`run_api.py`、`setup.py`，但缺少指南 §5 要求的 `alembic.ini` 与 `alembic/` 两行 COPY。
   - 已确认构建出的镜像里 `/app/alembic.ini` 和 `/app/alembic` 都不存在（WORKDIR=`/app`），执行 `alembic upgrade head` 会报 "No 'script_location' key found"。
   - 数据库没有 `alembic_version` 表——表结构当初用 `Base.metadata.create_all()` 兜底建的，从未迁移过。
   - 影响：对 v1.1.3 无影响；但以后任何带迁移的版本将无法迁移，且 `create_all()` 不会给已有表加列 → 可能出现 `column ... does not exist` 运行时错误。

2. **🟡 当前 schema 完整性未核实。** 尚未在服务器上核对 `files.document_type`、`document_chunks.{page_num_int,position_int,top_int}` 等列是否齐全。

3. **🟡 ES BM25 补索引缺失。** 如果服务器之前一直跑 `localhost:9200`（ES 实际失效），则之前上传的文档不在 BM25 关键词索引里；现在只有新上传的文档会入索引，旧文档未补索引。向量检索（Milvus）不受影响。

4. **🟢 服务器缺 `migrate-openrag` 辅助脚本。** 服务器上的 `deploy-openrag-compose-release.sh` 是旧版本，只生成 `dc-openrag`，不生成 `migrate-openrag`。

## 备注
- 本文档不含任何密钥。
- `main` 已本地合并，未推送到 `origin`。
