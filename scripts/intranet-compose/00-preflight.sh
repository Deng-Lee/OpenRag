#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

echo "[preflight 1/7] 校验离线制品"
require_file "$SOURCE_ZIP"
require_file "$SOURCE_ZIP.sha256"
require_file "$IMAGE_TAR"
require_file "$IMAGE_TAR.sha256"
require_file "$BUNDLE_DIR/openrag-$DEPLOY_VERSION-manifest.json"
require_file "$BUNDLE_DIR/openrag-offline-manifest.json"

(
  cd "$BUNDLE_DIR"
  sha256sum -c "$(basename "$SOURCE_ZIP.sha256")"
  sha256sum -c "$(basename "$IMAGE_TAR.sha256")"
  if [ "${THIRD_PARTY_INCLUDED:-0}" = "1" ]; then
    require_file "$THIRD_PARTY_TAR"
    require_file "$THIRD_PARTY_TAR.sha256"
    sha256sum -c "$(basename "$THIRD_PARTY_TAR.sha256")"
  fi
)

echo "[preflight 2/7] 校验 Docker 和 Compose"
docker --version
docker compose version
docker info >/dev/null

echo "[preflight 3/7] 校验 Compose !override 支持"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
cat >"$tmp_dir/base.yml" <<'YAML'
services:
  probe:
    image: busybox
    ports:
      - "8001:1"
YAML
cat >"$tmp_dir/override.yml" <<'YAML'
services:
  probe:
    ports: !override
      - "127.0.0.1:18001:1"
YAML
docker compose -f "$tmp_dir/base.yml" -f "$tmp_dir/override.yml" config >/dev/null

echo "[preflight 4/7] 校验服务器目录和运行时环境文件"
if [ ! -d "$SERVER_HOME" ] || [ ! -d "$SHARED_DIR" ]; then
  echo "服务器部署目录或 shared 目录不存在: $SERVER_HOME" >&2
  exit 1
fi
require_file "$ENV_FILE"
required_env_keys=(
  POSTGRES_PASSWORD SECRET_KEY MINIO_ROOT_USER MINIO_ROOT_PASSWORD
  OPENAI_API_KEY OPENAI_BASE_URL EMBEDDING_MODEL EMBEDDING_DIMENSION
)
for key in "${required_env_keys[@]}"; do
  status="$(env_key_status "$key")"
  printf '%s=%s\n' "$key" "$status"
  if [ "$status" != "SET" ]; then
    echo "运行时配置未就绪: $key=$status" >&2
    exit 1
  fi
done
if grep -q $'\r$' "$ENV_FILE"; then
  echo "提示: openrag.env 包含 CRLF，将在 prepare 阶段转换为 LF。"
fi

echo "[preflight 5/7] 校验第三方镜像"
if [ "${THIRD_PARTY_INCLUDED:-0}" != "1" ]; then
  third_party_images=(
    postgres:16-alpine
    quay.io/coreos/etcd:v3.5.5
    minio/minio:RELEASE.2023-03-20T20-16-18Z
    milvusdb/milvus:v2.4.17
    docker.elastic.co/elasticsearch/elasticsearch:8.12.2
  )
  for image in "${third_party_images[@]}"; do
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      echo "内网缺少第三方镜像且离线包未携带: $image" >&2
      exit 1
    fi
  done
else
  echo "离线包包含第三方镜像，将在 prepare 阶段导入。"
fi

# 如果这是首次部署，离线包未选择的应用镜像也必须已存在；内网不会临时构建。
if ! docker container inspect openrag-postgres-prod >/dev/null 2>&1; then
  for service in api web task-worker; do
    if [[ " $SELECTED_SERVICES " != *" $service "* ]]; then
      image="$(service_image "$service")"
      if ! docker image inspect "$image:latest" >/dev/null 2>&1; then
        echo "首次部署缺少未打包的应用镜像: $image:latest" >&2
        echo "请在外网重新构建包含 api,web,task-worker 的完整应用包。" >&2
        exit 1
      fi
    fi
  done
fi

echo "[preflight 6/7] 校验磁盘空间"
bundle_kb="$(du -sk "$BUNDLE_DIR" | awk '{print $1}')"
available_kb="$(df -Pk "$SERVER_HOME" | awk 'NR==2 {print $4}')"
required_kb=$((bundle_kb * 2 + 1048576))
printf 'bundle_kb=%s available_kb=%s required_kb=%s\n' \
  "$bundle_kb" "$available_kb" "$required_kb"
if [ "$available_kb" -lt "$required_kb" ]; then
  echo "磁盘空间不足，至少需要离线包两倍空间再加 1 GiB。" >&2
  exit 1
fi

echo "[preflight 7/7] 校验发布状态"
if [ -L "$CURRENT_LINK" ] && [ ! -e "$CURRENT_LINK" ]; then
  echo "current 是失效符号链接，无法建立可靠回退点: $CURRENT_LINK" >&2
  exit 1
fi
if [ ! -e "$CURRENT_LINK" ] && \
   docker container inspect openrag-api-prod >/dev/null 2>&1; then
  echo "检测到正在部署过的 API 容器，但缺少 $CURRENT_LINK。" >&2
  echo "无法证明旧源码版本，拒绝执行不可验证的自动回退；请先恢复 current 指针。" >&2
  exit 1
fi
if [ -e "$RELEASE_DIR" ]; then
  echo "版本目录已经存在，拒绝覆盖不可变发布: $RELEASE_DIR" >&2
  exit 1
fi
if [ -e "$BACKUP_DIR/prepared.ok" ]; then
  echo "该版本已经执行过 prepare，拒绝覆盖回退状态: $BACKUP_DIR" >&2
  exit 1
fi

echo "preflight-ok"
