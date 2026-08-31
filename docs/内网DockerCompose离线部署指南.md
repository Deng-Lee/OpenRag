# OpenRag 内网 Docker Compose 离线部署指南

本流程用于“外网开发和构建、制品人工搬运到内网、内网 Docker Compose 部署”。内网服务器不执行 `git pull`、`docker build`、`docker pull`、`pip install` 或 `npm install`。

## 1. 安全边界

- 源码和应用镜像都在外网构建机生成。
- 离线包不包含 `docker/.env`、`openrag.env` 或其他运行时密钥。
- 内网继续使用 `$SERVER_HOME/shared/openrag.env`。
- 不执行 `docker compose down -v`，不删除命名卷。
- 若存在 `$SERVER_HOME/shared/docker-compose.data-active.yml`，所有 Compose 和迁移命令都会继续加载它。
- 发布目录不可覆盖；同一版本不能重复 prepare，请使用新版本号。
- prepare 阶段先保存旧容器镜像 ID、`current` 指针和共享辅助文件，再导入新镜像。
- 不包含数据库迁移时，自动流程在部署或验证失败后回退应用镜像和源码。
- 包含数据库迁移时，脚本会先执行 `pg_dump`，但默认禁止自动假定数据库 schema 可以降级。

## 2. 外网构建离线包

在干净的本地 `deploy-main` worktree 中执行：

```powershell
cd E:\project\OpenRag\.worktrees\deploy-main-deploy-1.1.8

.\scripts\build-openrag-intranet-release.ps1 `
  -Version "<版本号>" `
  -BaseRef "<内网当前部署对应的提交或标签>" `
  -Services api,task-worker
```

当前 Excel 解析改动影响共享后端与 worker，因此至少构建 `api,task-worker`。如果不能证明内网已有兼容的 Web 镜像，改用：

```powershell
-Services api,web,task-worker
```

首次部署或内网缺少第三方镜像时增加：

```powershell
-IncludeThirdPartyImages
```

只有变更包含 Alembic 迁移并经过评审时才增加：

```powershell
-RequiresMigration
```

成功输出：

```text
offline-release-build-ok
```

生成两个需要搬运的文件：

```text
artifacts/openrag-offline-<版本号>.tar
artifacts/openrag-offline-<版本号>.tar.sha256
```

## 3. 搬运到内网

可以使用获批的 SFTP 通道。两个文件必须一起搬运；不要单独修改 tar 内的文件。

在内网 Linux 服务器先校验外层包：

```bash
sha256sum -c openrag-offline-<版本号>.tar.sha256
tar -xf openrag-offline-<版本号>.tar
cd openrag-offline-<版本号>
```

预期 sha256 输出 `OK`。

## 4. 一键执行

50 节点生产目录的大小写是：

```bash
export SERVER_HOME=/home/guozhi/Documents/openrag
```

33 节点测试目录的大小写是：

```bash
export SERVER_HOME=/home/guozhi/Documents/OpenRag
```

然后执行：

```bash
SERVER_HOME="$SERVER_HOME" bash scripts/deploy-openrag-offline.sh
```

只有最后输出以下内容才算自动部署完成：

```text
offline-deploy-ok
```

部署报告位于：

```text
$SERVER_HOME/artifacts/<版本号>/deploy-report-<版本号>.md
```

诊断日志位于：

```text
$SERVER_HOME/artifacts/<版本号>/deploy-checks-<版本号>.log
```

## 5. 分阶段执行

如果希望每一步人工确认，可依次执行。

### 阶段 0：只读预检

```bash
SERVER_HOME="$SERVER_HOME" bash scripts/00-preflight.sh
```

预期输出：

```text
preflight-ok
```

它会检查 sha256、Docker/Compose、`!override`、运行时环境项状态、第三方镜像、磁盘空间和版本目录冲突，不启动或停止服务。

### 阶段 1：准备发布

```bash
SERVER_HOME="$SERVER_HOME" bash scripts/10-prepare.sh
```

预期输出：

```text
prepare-ok
```

它会保存回退状态、导入版本化镜像、解压新源码、运行镜像依赖/Excel 切分冒烟测试，并对新 Compose 做静态合并检查；不会切换线上 `current`。校验后的源码包和镜像包会移动到 `$SERVER_HOME/artifacts/<版本号>/` 留档，避免重复占用磁盘。

### 阶段 2：应用发布

```bash
SERVER_HOME="$SERVER_HOME" bash scripts/20-deploy.sh
```

预期输出：

```text
deploy-applied
```

它会切换 `current`，保留现有数据卷策略，并仅重建包内声明的应用服务。这个输出不代表验收完成。

### 阶段 3：自动验证

```bash
SERVER_HOME="$SERVER_HOME" bash scripts/30-verify.sh
```

预期输出：

```text
verification-ok
```

验证包括：核心容器、实际运行镜像 ID、Web/API 三条健康链路、`app-config.js`、Alembic 版本，以及 worker 中真实生成的超宽/超长 Excel token 切分冒烟测试。

## 6. 回退

无数据库迁移时执行：

```bash
SERVER_HOME="$SERVER_HOME" bash scripts/90-rollback.sh
```

预期输出：

```text
rollback-ok
```

脚本会恢复 prepare 阶段记录的镜像 ID 和旧源码指针，重建受影响服务，再检查三条基础健康链路。

如果发布运行过数据库迁移，脚本会默认停止并指出迁移前备份位置。只有确认旧应用兼容新 schema 时，才允许仅回退应用：

```bash
ALLOW_APP_ONLY_ROLLBACK_AFTER_MIGRATION=1 \
SERVER_HOME="$SERVER_HOME" \
bash scripts/90-rollback.sh
```

恢复数据库备份会覆盖数据，不属于自动回退范围，必须单独审批和演练。

## 7. 人工业务验收

自动验证通过后仍需完成：

1. 浏览器登录并打开目标 Workspace。
2. 上传此前失败的大 Excel，确认文档处理成功。
3. 检查 Chunk token 上限和 Sheet 层级导航。
4. 执行一次知识库检索，确认 Elasticsearch 和 Milvus 结果正常。
5. 将部署报告和检查日志通过 SFTP 搬回外网留档，避免后续只能依赖截图排障。
