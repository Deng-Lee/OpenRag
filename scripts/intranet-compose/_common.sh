#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RELEASE_ENV="$BUNDLE_DIR/release.env"

if [ ! -s "$RELEASE_ENV" ]; then
  echo "缺少发布描述文件: $RELEASE_ENV" >&2
  exit 1
fi

# release.env 由外网构建脚本生成，只包含版本、提交和镜像清单，不包含密钥。
# shellcheck disable=SC1090
source "$RELEASE_ENV"

: "${DEPLOY_VERSION:?release.env 缺少 DEPLOY_VERSION}"
: "${GIT_COMMIT:?release.env 缺少 GIT_COMMIT}"
: "${SELECTED_SERVICES:?release.env 缺少 SELECTED_SERVICES}"
: "${REQUIRES_MIGRATION:?release.env 缺少 REQUIRES_MIGRATION}"
: "${SERVER_HOME:?请设置 SERVER_HOME，例如 /home/guozhi/Documents/openrag}"

case "$SERVER_HOME" in
  /*) ;;
  *) echo "SERVER_HOME 必须是绝对路径: $SERVER_HOME" >&2; exit 1 ;;
esac
if [ "$SERVER_HOME" = "/" ] || [ ${#SERVER_HOME} -lt 8 ]; then
  echo "拒绝使用不安全的 SERVER_HOME: $SERVER_HOME" >&2
  exit 1
fi

API_PORT="${API_PORT:-18001}"
WEB_PORT="${WEB_PORT:-80}"
if [ "$WEB_PORT" = "80" ]; then
  WEB_ORIGIN="http://127.0.0.1"
else
  WEB_ORIGIN="http://127.0.0.1:$WEB_PORT"
fi
ARTIFACT_DIR="$SERVER_HOME/artifacts/$DEPLOY_VERSION"
RELEASES_DIR="$SERVER_HOME/releases"
RELEASE_DIR="$RELEASES_DIR/openrag-$DEPLOY_VERSION"
SHARED_DIR="$SERVER_HOME/shared"
BACKUP_DIR="$SERVER_HOME/backups/deploy-$DEPLOY_VERSION"
CURRENT_LINK="$SERVER_HOME/current"
ENV_FILE="$SHARED_DIR/openrag.env"
STATE_FILE="$BACKUP_DIR/state.env"
REPORT_FILE="$ARTIFACT_DIR/deploy-report-$DEPLOY_VERSION.md"
CHECK_LOG="$ARTIFACT_DIR/deploy-checks-$DEPLOY_VERSION.log"
SOURCE_ZIP="$BUNDLE_DIR/openrag-$DEPLOY_VERSION-src.zip"
IMAGE_TAR="$BUNDLE_DIR/openrag-selected-images-$DEPLOY_VERSION.tar"
THIRD_PARTY_TAR="$BUNDLE_DIR/openrag-third-party-images.tar"

read -r -a SELECTED_SERVICE_LIST <<<"$SELECTED_SERVICES"

service_image() {
  case "$1" in
    api) echo "openrag-api" ;;
    web) echo "openrag-web" ;;
    task-worker) echo "openrag-task-worker" ;;
    *) echo "未知应用服务: $1" >&2; return 1 ;;
  esac
}

service_container() {
  case "$1" in
    api) echo "openrag-api-prod" ;;
    web) echo "openrag-web-prod" ;;
    task-worker) echo "openrag-task-worker" ;;
    *) echo "未知应用服务: $1" >&2; return 1 ;;
  esac
}

require_file() {
  if [ ! -s "$1" ]; then
    echo "缺少必需文件: $1" >&2
    exit 1
  fi
}

write_state() {
  local key="$1"
  local value="$2"
  printf '%s=%q\n' "$key" "$value" >>"$STATE_FILE"
}

env_key_status() {
  local key="$1"
  if ! grep -qE "^[[:space:]]*${key}=" "$ENV_FILE"; then
    echo "MISSING"
  elif grep -qE "^[[:space:]]*${key}=[[:space:]]*$" "$ENV_FILE"; then
    echo "EMPTY"
  else
    echo "SET"
  fi
}

compose_args() {
  COMPOSE_ARGS=(
    -p openrag
    --env-file "$ENV_FILE"
    -f "$CURRENT_LINK/docker/docker-compose.prod.yml"
    -f "$SHARED_DIR/docker-compose.server.yml"
  )
  if [ -s "$SHARED_DIR/docker-compose.data-active.yml" ]; then
    COMPOSE_ARGS+=(-f "$SHARED_DIR/docker-compose.data-active.yml")
  fi
}

dc() {
  compose_args
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

render_server_override() {
  local target="$1"
  local app_config_path="$2"
  cat >"$target" <<YAML
services:
  api:
    ports: !override
      - "127.0.0.1:\${API_PORT:-$API_PORT}:8000"
    environment:
      - STORAGE_ENDPOINT=milvus-minio:9000
      - STORAGE_ACCESS_KEY=\${MINIO_ROOT_USER:-minioadmin}
      - STORAGE_SECRET_KEY=\${MINIO_ROOT_PASSWORD:-minioadmin}
      - HIERARCHY_STORAGE_PATH=/app/storage/hierarchies
    volumes:
      - hierarchy-data:/app/storage/hierarchies

  task-worker:
    environment:
      - STORAGE_ENDPOINT=milvus-minio:9000
      - STORAGE_ACCESS_KEY=\${MINIO_ROOT_USER:-minioadmin}
      - STORAGE_SECRET_KEY=\${MINIO_ROOT_PASSWORD:-minioadmin}
      - HIERARCHY_STORAGE_PATH=/app/storage/hierarchies
    volumes:
      - hierarchy-data:/app/storage/hierarchies

  milvus:
    ports: !override
      - "127.0.0.1:19530:19530"
      - "127.0.0.1:9091:9091"
    environment:
      MINIO_ACCESS_KEY_ID: \${MINIO_ROOT_USER:-minioadmin}
      MINIO_SECRET_ACCESS_KEY: \${MINIO_ROOT_PASSWORD:-minioadmin}

  milvus-minio:
    ports: !override
      - "127.0.0.1:9001:9001"
      - "127.0.0.1:9000:9000"

  postgres:
    ports: !override
      - "127.0.0.1:5432:5432"

  elasticsearch:
    ports: !override
      - "127.0.0.1:\${ELASTICSEARCH_PORT:-9200}:9200"

  trace-dashboard:
    profiles:
      - trace-dashboard
    ports: !override
      - "127.0.0.1:\${TRACE_DASHBOARD_PORT:-8501}:8501"

  web:
    ports: !override
      - "\${WEB_PORT:-$WEB_PORT}:80"
    volumes:
      - $app_config_path:/usr/share/nginx/html/app-config.js:ro

volumes:
  hierarchy-data:
    driver: local
YAML
}

write_shared_helpers() {
  mkdir -p "$SHARED_DIR"
  cat >"$SHARED_DIR/app-config.js" <<'JS'
window.__OPENRAG_CONFIG__ = {
  apiBaseUrl: "/api"
};
JS
  render_server_override \
    "$SHARED_DIR/docker-compose.server.yml" \
    "$SHARED_DIR/app-config.js"

  cat >"$SHARED_DIR/dc-openrag" <<EOF
#!/usr/bin/env bash
set -euo pipefail
SERVER_HOME="$SERVER_HOME"
CURRENT_LINK="\$SERVER_HOME/current"
SHARED_DIR="\$SERVER_HOME/shared"
args=(-p openrag --env-file "\$SHARED_DIR/openrag.env" -f "\$CURRENT_LINK/docker/docker-compose.prod.yml" -f "\$SHARED_DIR/docker-compose.server.yml")
if [ -s "\$SHARED_DIR/docker-compose.data-active.yml" ]; then
  args+=(-f "\$SHARED_DIR/docker-compose.data-active.yml")
fi
exec docker compose "\${args[@]}" "\$@"
EOF

  cat >"$SHARED_DIR/migrate-openrag" <<EOF
#!/usr/bin/env bash
set -euo pipefail
SERVER_HOME="$SERVER_HOME"
CURRENT_LINK="\$SERVER_HOME/current"
SHARED_DIR="\$SERVER_HOME/shared"
args=(-p openrag --env-file "\$SHARED_DIR/openrag.env" -f "\$CURRENT_LINK/docker/docker-compose.prod.yml" -f "\$SHARED_DIR/docker-compose.server.yml")
if [ -s "\$SHARED_DIR/docker-compose.data-active.yml" ]; then
  args+=(-f "\$SHARED_DIR/docker-compose.data-active.yml")
fi
exec docker compose "\${args[@]}" run --rm --no-deps api python -m alembic upgrade head
EOF
  chmod +x "$SHARED_DIR/dc-openrag" "$SHARED_DIR/migrate-openrag"
}

wait_http() {
  local name="$1"
  local url="$2"
  local attempts="${3:-30}"
  local index
  for ((index = 1; index <= attempts; index++)); do
    if curl -fsS --max-time 5 "$url" >/dev/null; then
      echo "OK: $name ($url)"
      return 0
    fi
    sleep 4
  done
  echo "FAIL: $name ($url)" >&2
  return 1
}

append_report() {
  mkdir -p "$ARTIFACT_DIR"
  printf '%s\n' "$1" | tee -a "$REPORT_FILE"
}
