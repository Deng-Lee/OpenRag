#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

echo "[prepare 1/7] 建立版本化目录和回退状态"
mkdir -p "$ARTIFACT_DIR" "$RELEASES_DIR" "$SHARED_DIR" "$BACKUP_DIR"
chmod 600 "$ENV_FILE"
sed -i 's/\r$//' "$ENV_FILE"
if [ -e "$BACKUP_DIR/prepared.ok" ] || [ -e "$RELEASE_DIR" ]; then
  echo "该版本已经准备过，拒绝覆盖: $DEPLOY_VERSION" >&2
  exit 1
fi
: >"$STATE_FILE"
chmod 600 "$STATE_FILE"
previous_release=""
if [ -L "$CURRENT_LINK" ]; then
  previous_release="$(readlink -f "$CURRENT_LINK")"
elif [ -e "$CURRENT_LINK" ]; then
  previous_release="$CURRENT_LINK"
fi
write_state "PREVIOUS_RELEASE" "$previous_release"
write_state "MIGRATION_RAN" "0"
write_state "SELECTED_SERVICES" "$SELECTED_SERVICES"

for helper in app-config.js docker-compose.server.yml dc-openrag migrate-openrag; do
  key="$(printf '%s' "$helper" | tr '[:lower:].-' '[:upper:]__')_EXISTED"
  if [ -e "$SHARED_DIR/$helper" ]; then
    cp -a "$SHARED_DIR/$helper" "$BACKUP_DIR/$helper"
    write_state "$key" "1"
  else
    write_state "$key" "0"
  fi
done

echo "[prepare 2/7] 保存当前运行镜像以供回退"
for service in "${SELECTED_SERVICE_LIST[@]}"; do
  image="$(service_image "$service")"
  container="$(service_container "$service")"
  image_id=""
  if docker container inspect "$container" >/dev/null 2>&1; then
    image_id="$(docker container inspect --format '{{.Image}}' "$container")"
  elif docker image inspect "$image:latest" >/dev/null 2>&1; then
    image_id="$(docker image inspect --format '{{.Id}}' "$image:latest")"
  fi
  state_key="$(printf '%s' "$service" | tr '[:lower:]-' '[:upper:]_')_IMAGE_ID"
  write_state "$state_key" "$image_id"
  if [ -n "$image_id" ]; then
    docker tag "$image_id" "$image:rollback-$DEPLOY_VERSION"
    echo "已保存 $service 回退镜像: $image_id"
  else
    echo "未找到 $service 的旧镜像；按首次部署处理。"
  fi
done

echo "[prepare 3/7] 再次校验并导入镜像"
(
  cd "$BUNDLE_DIR"
  sha256sum -c "$(basename "$SOURCE_ZIP.sha256")"
  sha256sum -c "$(basename "$IMAGE_TAR.sha256")"
)
if [ "${THIRD_PARTY_INCLUDED:-0}" = "1" ]; then
  docker load -i "$THIRD_PARTY_TAR"
fi
docker load -i "$IMAGE_TAR"
for service in "${SELECTED_SERVICE_LIST[@]}"; do
  image="$(service_image "$service")"
  docker image inspect "$image:$DEPLOY_VERSION" >/dev/null
  docker tag "$image:$DEPLOY_VERSION" "$image:latest"
done

echo "[prepare 4/7] 解压不可变源码发布"
release_tmp="$RELEASE_DIR.tmp.$$"
rm -rf "$release_tmp"
mkdir -p "$release_tmp"
if command -v unzip >/dev/null 2>&1; then
  unzip -q "$SOURCE_ZIP" -d "$release_tmp"
elif command -v python3 >/dev/null 2>&1; then
  python3 -m zipfile -e "$SOURCE_ZIP" "$release_tmp"
else
  echo "服务器既没有 unzip 也没有 python3，无法解压源码包。" >&2
  exit 1
fi
test -s "$release_tmp/docker/docker-compose.prod.yml"
mv "$release_tmp" "$RELEASE_DIR"

echo "[prepare 5/7] 校验新镜像运行时"
for service in "${SELECTED_SERVICE_LIST[@]}"; do
  image="$(service_image "$service"):$DEPLOY_VERSION"
  case "$service" in
    api)
      docker run --rm "$image" sh -c \
        "test -f /app/alembic.ini && test -d /app/alembic && python -m alembic --help >/dev/null"
      docker run --rm "$image" python -c \
        "import openrag.api.main, numpy, pymilvus; print('api-import-ok')"
      ;;
    web)
      docker run --rm "$image" nginx -t
      ;;
    task-worker)
      docker run --rm "$image" python -c \
        "import python_calamine, tiktoken; from openrag.parsers.adapters.excel_adapter import StructuredExcelParserAdapter; from openrag.chunking.excel_table_chunker import split_excel_blocks; print('worker-excel-import-ok')"
      docker run --rm -i "$image" python - <"$SCRIPT_DIR/verify_excel_table_chunking.py"
      ;;
  esac
done

echo "[prepare 6/7] 校验新源码与现有环境的 Compose 合并结果"
prepare_app_config="$BACKUP_DIR/app-config.prepare.js"
prepare_override="$BACKUP_DIR/docker-compose.prepare.yml"
printf '%s\n' 'window.__OPENRAG_CONFIG__ = { apiBaseUrl: "/api" };' >"$prepare_app_config"
render_server_override "$prepare_override" "$prepare_app_config"
compose_check=(
  -p openrag
  --env-file "$ENV_FILE"
  -f "$RELEASE_DIR/docker/docker-compose.prod.yml"
  -f "$prepare_override"
)
if [ -s "$SHARED_DIR/docker-compose.data-active.yml" ]; then
  compose_check+=(-f "$SHARED_DIR/docker-compose.data-active.yml")
fi
docker compose "${compose_check[@]}" config --quiet
docker compose "${compose_check[@]}" config --services

echo "[prepare 7/7] 保存审计文件"
cp -f "$BUNDLE_DIR/openrag-offline-manifest.json" "$ARTIFACT_DIR/"
cp -f "$BUNDLE_DIR/openrag-$DEPLOY_VERSION-manifest.json" "$ARTIFACT_DIR/"
cp -f "$BUNDLE_DIR/deploy-plan-$DEPLOY_VERSION.md" "$ARTIFACT_DIR/"
mv "$SOURCE_ZIP" "$SOURCE_ZIP.sha256" "$ARTIFACT_DIR/"
mv "$IMAGE_TAR" "$IMAGE_TAR.sha256" "$ARTIFACT_DIR/"
if [ "${THIRD_PARTY_INCLUDED:-0}" = "1" ]; then
  mv "$THIRD_PARTY_TAR" "$THIRD_PARTY_TAR.sha256" "$ARTIFACT_DIR/"
fi
printf 'prepared_at=%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >"$BACKUP_DIR/prepared.ok"

echo "prepare-ok"
echo "release=$RELEASE_DIR"
echo "rollback_state=$STATE_FILE"
