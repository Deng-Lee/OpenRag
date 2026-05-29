# OpenRag 服务器 Docker 部署排障参考

## 版本号和产物

症状：`sha256sum -c openrag-${DEPLOY_VERSION}-src.zip.sha256` 找不到文件。

原因：Linux 文件名大小写敏感，`V1.0.0` 和 `1.0.0` 不同。

处理：

```bash
ls -lh /home/guozhi/Documents/OpenRag/artifacts
echo "$DEPLOY_VERSION"
```

确保变量和文件名完全一致。

症状：没有 manifest。

处理：本地重新运行 `package-openrag-release.ps1 -Version <version>`，或按现有 zip 重新生成 manifest。

## 服务器无法构建镜像

症状：

```text
failed to resolve source metadata for docker.io/library/python:3.11-slim
i/o timeout
```

原因：服务器无法访问 Docker Hub。

处理：本地构建并 `docker save`，服务器 `docker load`，启动时使用 `--no-build`。

症状：

```text
Unable to connect to deb.debian.org
Unable to locate package gcc
```

原因：服务器构建镜像时访问 Debian 软件源失败。

处理：同样改成本地构建镜像后上传，不要让服务器 build。

## `dc` / `dc_release` 不存在

症状：

```text
dc_release: command not found
```

原因：shell 函数只在当前 SSH 会话有效。

处理：重新粘贴函数定义：

```bash
dc() {
  docker compose -p openrag \
    --env-file "$SHARED_DIR/openrag.env" \
    -f "$CURRENT_LINK/docker/docker-compose.prod.yml" \
    -f "$SHARED_DIR/docker-compose.server.yml" \
    "$@"
}
```

## 端口占用

症状：

```text
failed to bind host port 0.0.0.0:8001/tcp: address already in use
```

处理：

```bash
sudo ss -ltnp | grep ':8001'
sudo ss -ltnp | grep ':18001' || echo "18001 is free"
```

如果占用应用不能停，使用 `API_PORT=18001`。

推荐 override：

```yaml
services:
  api:
    ports: !override
      - "${API_PORT:-18001}:8000"
```

如果 Docker Compose 不支持 `!override`，临时 patch 当前 release：

```bash
sed -i 's|"8001:8000"|"18001:8000"|' "$CURRENT_LINK/docker/docker-compose.prod.yml"
```

然后：

```bash
dc up -d --no-build
```

## `app-config.js` 404 或登录请求跑到 localhost

症状：

```text
GET /app-config.js 404
```

或浏览器 Network 中登录请求为：

```text
http://localhost:8001/users/login
```

处理：

```bash
cat > /home/guozhi/Documents/OpenRag/shared/app-config.js <<'EOF'
window.__OPENRAG_CONFIG__ = {
  apiBaseUrl: "/api"
};
EOF
```

在 override 中挂载：

```yaml
services:
  web:
    volumes:
      - /home/guozhi/Documents/OpenRag/shared/app-config.js:/usr/share/nginx/html/app-config.js:ro
```

重启并验证：

```bash
dc up -d --no-build web
curl -i http://127.0.0.1/app-config.js
```

浏览器强刷 `Ctrl+F5`。

## 登录返回 401

含义：网络和后端链路已经通了，但后端认证失败。

后端登录只认 email，不认 username。

查用户：

```bash
docker exec openrag-postgres-prod psql -U openrag -d openrag -c "select id, username, email, is_active, is_admin from users;"
```

如果没有用户，打开：

```text
http://<server-ip>/register
```

如果用户存在但密码不确定，重置密码：

```bash
read -rp "请输入要重置的邮箱: " USER_EMAIL
read -rsp "请输入新密码: " USER_PASSWORD
echo
docker exec -e USER_EMAIL="$USER_EMAIL" -e USER_PASSWORD="$USER_PASSWORD" openrag-api-prod python -c "from openrag.database import SessionLocal; from openrag.services.user_manager import UserManager; import os; db=SessionLocal(); mgr=UserManager(db); user=mgr.get_user_by_email(os.environ['USER_EMAIL']); assert user is not None, 'user not found'; mgr.update_user(user.id, password=os.environ['USER_PASSWORD'], is_active=True); print('password reset ok for', user.email)"
```

## MinIO / Milvus 凭据

症状：Milvus 或 Worker 日志出现 MinIO 认证失败、AccessDenied。

原因：`docker-compose.prod.yml` 中 Milvus 原始配置可能硬编码 `minioadmin/minioadmin`，但生产 `.env` 改了密码。

处理：server override 必须覆盖：

```yaml
services:
  milvus:
    environment:
      MINIO_ACCESS_KEY_ID: ${MINIO_ROOT_USER:-minioadmin}
      MINIO_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD:-minioadmin}
```

API/Worker 必须有：

```yaml
STORAGE_ENDPOINT=milvus-minio:9000
STORAGE_ACCESS_KEY=${MINIO_ROOT_USER:-minioadmin}
STORAGE_SECRET_KEY=${MINIO_ROOT_PASSWORD:-minioadmin}
```
