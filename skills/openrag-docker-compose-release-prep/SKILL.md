---
name: openrag-docker-compose-release-prep
description: 辅助从当前 OpenRag 项目工作区准备并上传外网 Docker Compose 发布制品。用于用户要求按 docs/外网DockerCompose部署指南.md 的第 1 到第 7 步执行：询问发布版本号，检查本地仓库和 docker/.env，校验 Compose，本地构建和打包源码及镜像，校验 worker 离线模型，并把制品和 openrag.env 上传到 Linux 服务器。不用于服务器端部署、启动服务或迁移数据库。
---

# OpenRag Docker Compose 发布准备

## 范围

只执行 `docs/外网DockerCompose部署指南.md` 中第 1 到第 7 步，也就是本地准备、构建打包、校验和上传。

不要执行第 8 步及之后的服务器部署动作。不要在服务器上运行 `docker compose up`，不要在服务器上构建镜像，不要执行 `git reset` 或其他破坏性 Git 操作。

## 必要输入

如果用户没有给出版本号，只问这一个问题：

```text
本次发布版本号是多少？请使用类似 1.0.1 的格式，不要带 V 前缀。
```

拒绝以 `V` 或 `v` 开头的版本号。除非用户明确提供替代值，否则使用以下默认值：

- `SshTarget`: `guozhi@192.168.100.33`
- `ServerHome`: `/home/guozhi/Documents/OpenRag`
- `ApiPort`: `18001`
- `WebPort`: `80`
- `EnvFile`: `docker\.env`

## 执行规则

- 从当前项目根目录执行，通常是 `E:\project\OpenRag`。
- 执行前先阅读 `docs/外网DockerCompose部署指南.md`。如果找不到精确路径，用 `rg --files docs | rg "外网.*DockerCompose.*部署指南|外网.*docker.*compose.*部署指南"` 定位。
- 使用本 Skill 自带脚本 `scripts/prepare-openrag-compose-release.ps1` 执行流程。
- 不要打印真实 `.env` 密钥。必需环境变量只汇总为 `SET`、`EMPTY` 或 `MISSING`。
- 任一步失败就停止，说明失败步骤和下一步修正建议。
- 默认使用非交互 SSH/SCP。若出现 `Permission denied (publickey,password)` 或连接超时，提示用户配置免密 SSH，或在已登录的终端中手动执行脚本打印出的上传命令。
- 默认重新构建应用镜像。只有用户明确要求复用本地现有镜像时，才添加 `-SkipImageBuild`。
- 默认复用已有第三方镜像包，不自动重新构建第三方镜像。只有用户明确要求重新生成第三方镜像包时，才添加 `-ForceThirdPartyImages`。
- 如果本地已存在 `artifacts/openrag-third-party-images.tar` 和对应 sha256 文件，默认一并上传；如果不存在，则跳过第三方镜像包上传。
- 默认会把 `docker/.env` 上传覆盖服务器的 `shared/openrag.env`。外网 192.168.100.33 发布应使用 `-EnvFile docker\.env.external-192.168.100.33`。当本次为仅代码变更、`.env` 未改时，添加 `-SkipEnvUpload` 跳过该上传，保留服务器现有运行配置，避免误覆盖生产密钥。

## 工作流

1. 确认版本号和默认服务器参数。
2. 从仓库根目录运行项目内脚本：

```powershell
& ".\skills\openrag-docker-compose-release-prep\scripts\prepare-openrag-compose-release.ps1" `
  -Version "1.0.1" `
  -SshTarget "guozhi@192.168.100.33" `
  -ServerHome "/home/guozhi/Documents/OpenRag" `
  -ApiPort "18001" `
  -WebPort "80" `
  -EnvFile "docker\.env.external-192.168.100.33"
```

3. 如果用户明确要求复用现有应用镜像，添加 `-SkipImageBuild`。
4. 如果用户明确要求重新生成第三方镜像包，添加 `-ForceThirdPartyImages`。

## 成功标准

只有同时满足以下条件，才报告成功：

- 必需文件和 `.env` 必需项检查通过。
- SSH 目标可访问。
- Docker 和 Docker Compose 可用。
- `docker compose config --quiet` 通过。
- 指定版本的发布制品已生成。
- `openrag-api`、`openrag-web`、`openrag-task-worker` 镜像存在。
- API 镜像包含 `/app/alembic.ini` 和 `/app/alembic`。
- worker 镜像包含必需 DeepDoc 离线模型文件。
- 应用制品和指定 `EnvFile` 已上传到服务器；如果本地已有第三方镜像包，也已一并上传。
- 远端校验输出 `upload-ok`。

结束时说明上传后的服务器路径：`<ServerHome>/artifacts` 和 `<ServerHome>/shared/openrag.env`。
