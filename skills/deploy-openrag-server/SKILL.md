---
name: deploy-openrag-server
description: 当用户要求把本地 OpenRag 项目通过 Docker 部署到外网或远程 Linux 服务器、上传本地代码而不是从 Git 拉取、发布到类似 192.168.100.33 的服务器，或排查 OpenRag Docker 部署、启动、app-config、登录、端口、MinIO、Milvus、离线镜像问题时使用。
---

# 部署 OpenRag 服务器

## 核心规则

始终从用户本地工作区部署。不要假设服务器可以 `git pull`，也不要假设服务器能稳定访问 Docker Hub、Debian 软件源、npm、pip 等外网资源。

优先采用本地构建镜像，然后通过 `docker save` / `docker load` 上传到服务器的方式。这个项目已经在服务器上遇到过 Docker Hub 超时和 Debian 源超时，所以不要默认让服务器构建镜像。

在这个仓库中始终使用中文回复。

## 最少输入

只询问缺失的必要信息：

- `version`：必填，例如 `1.0.1`；除非产物文件名也带 `V`，否则拒绝 `V1.0.1`。
- `ssh_target`：默认 `guozhi@192.168.100.33`。
- `server_home`：默认 `/home/guozhi/Documents/OpenRag`。
- `api_port`：默认 `18001`，用于避开已被占用的 `8001`；Web 仍然通过 `/api` 访问后端。
- `web_port`：默认 `80`。

不要打印真实 `.env` 密钥。涉及密钥时只汇总为 `SET`、`EMPTY` 或 `MISSING`。

## 执行流程

1. 检查本地状态：
   - 执行 `git status --short`
   - 阅读 Docker 和配置相关改动
   - 脱敏检查 `docker/.env`
   - 确认 `docker/docker-compose.prod.yml`、Dockerfile 和 `docker/.env` 存在
2. 使用本 skill 的 `scripts/package-openrag-release.ps1` 在本地打包。
3. 上传产物：
   - `openrag-<version>-src.zip`
   - `openrag-<version>-src.zip.sha256`
   - `openrag-<version>-manifest.json`
   - `openrag-images-<version>.tar`
   - `openrag-images-<version>.tar.sha256`
   - 将 `docker/.env` 上传为 `<server_home>/shared/openrag.env`
4. 在服务器创建目录：
   - `<server_home>/artifacts`
   - `<server_home>/releases`
   - `<server_home>/shared`
   - `<server_home>/backups`
5. 校验 sha256，并将源码包解压到 release 目录。
6. 在 `shared` 中创建服务器专用文件：
   - `openrag.env`，权限设为 `chmod 600`
   - `app-config.js`，内容设置 `apiBaseUrl: "/api"`
   - `docker-compose.server.yml` 覆盖配置
7. 使用 `docker load` 导入镜像包。启动时使用 `dc up -d --no-build`，不要在服务器上重新 build。
8. 验证部署：
   - `dc ps`
   - `curl -i http://127.0.0.1/health`
   - `curl -i http://127.0.0.1/api/health`
   - `curl -i http://127.0.0.1:<api_port>/health`
   - 浏览器访问 `http://<server_ip>/app-config.js` 返回 `200`
   - 登录请求必须发往 `/api/users/login`

## 本地打包

在仓库根目录执行：

```powershell
$Version = "1.0.1"
.\skills\deploy-openrag-server\scripts\package-openrag-release.ps1 -Version $Version
```

脚本会生成源码包、manifest、校验文件和完整离线镜像包。只有在应用镜像已经确认是最新时，才使用 `-SkipImageBuild`。

## 必需的服务器覆盖配置

需要生成 `/home/guozhi/Documents/OpenRag/shared/docker-compose.server.yml`，它负责：

- 将 `shared/app-config.js` 挂载到 Web 容器。
- 当 `8001` 被占用时，将 API 宿主机端口设置为 `${API_PORT:-18001}:8000`。
- 为 API 和 worker 设置 `STORAGE_ENDPOINT=milvus-minio:9000`。
- 将 `STORAGE_ACCESS_KEY` / `STORAGE_SECRET_KEY` 映射为 MinIO root 凭据。
- 覆盖 Milvus 的 MinIO 凭据，避免 Milvus 继续使用硬编码的 `minioadmin/minioadmin`。
- 使用 `hierarchy-data` 持久化层级文件。

如果 Docker Compose 不支持 `ports: !override`，读取 `references/troubleshooting.md` 中的降级方案：将当前 release 里的 compose 端口从 `8001:8000` 改成 `<api_port>:8000`。

## 常见问题

出现以下情况时，读取 `references/troubleshooting.md`：

- `python:3.11-slim failed to resolve source metadata`
- `Unable to connect to deb.debian.org`
- `dc_release: command not found`
- `address already in use 0.0.0.0:8001`
- `/app-config.js 404`
- 登录请求发到了 `localhost:8001`
- 登录返回 `401`
- MinIO 或 Milvus 凭据错误
- manifest 缺失
- 产物文件名缺少版本号

## 完成标准

满足以下条件后，才能说明部署完成：

- 所有容器都处于应有的 running 或 healthy 状态；
- `/health`、`/api/health` 和直连 API 健康检查都通过；
- `app-config.js` 返回 `200`；
- 登录请求能到达 `/api/users/login`；
- 如果登录返回 `401`，需要说明部署网络链路已经通了，并引导用户检查用户表、邮箱、激活状态和密码。
