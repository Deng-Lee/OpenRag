#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

require_file "$STATE_FILE"
# state.env 由 prepare 脚本使用 printf %q 生成。
# shellcheck disable=SC1090
source "$STATE_FILE"

if [ "${MIGRATION_RAN:-0}" = "1" ] && \
   [ "${ALLOW_APP_ONLY_ROLLBACK_AFTER_MIGRATION:-0}" != "1" ]; then
  echo "本次部署已经运行数据库迁移，默认拒绝只回退应用。" >&2
  echo "迁移前备份: $BACKUP_DIR/postgres-before-migration.sql.gz" >&2
  echo "请先评估 schema 向后兼容性；确认仅回退应用可行后，显式设置 ALLOW_APP_ONLY_ROLLBACK_AFTER_MIGRATION=1。" >&2
  exit 1
fi

echo "[rollback 1/5] 恢复旧应用镜像标签"
for service in "${SELECTED_SERVICE_LIST[@]}"; do
  image="$(service_image "$service")"
  state_key="$(printf '%s' "$service" | tr '[:lower:]-' '[:upper:]_')_IMAGE_ID"
  image_id="${!state_key-}"
  if [ -n "$image_id" ]; then
    docker image inspect "$image_id" >/dev/null
    docker tag "$image_id" "$image:latest"
    echo "已恢复 $service 镜像: $image_id"
  else
    echo "$service 没有旧镜像记录，将按首次部署回退处理。"
  fi
done

echo "[rollback 2/5] 恢复旧源码发布"
previous_release="${PREVIOUS_RELEASE:-}"
if [ -n "$previous_release" ] && [ -e "$previous_release" ]; then
  ln -sfn "$previous_release" "$CURRENT_LINK"
  test "$(readlink -f "$CURRENT_LINK")" = "$(readlink -f "$previous_release")"
else
  echo "没有旧源码发布；停止本次选择的应用服务。"
  if [ -L "$CURRENT_LINK" ]; then
    dc stop "${SELECTED_SERVICE_LIST[@]}" || true
    rm -f "$CURRENT_LINK"
  fi
  printf 'rolled_back_at=%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >"$BACKUP_DIR/rolled-back.ok"
  echo "rollback-ok (initial deployment stopped)"
  exit 0
fi

echo "[rollback 3/5] 使用旧源码和旧镜像重建受影响服务"
# 此时保留本次生成的 helper，确保即使旧部署没有 helper 也能安全启动旧版本。
dc config --quiet
dc up -d --no-build --no-deps --force-recreate "${SELECTED_SERVICE_LIST[@]}"

echo "[rollback 4/5] 校验回退后的基础健康状态"
wait_http "web health after rollback" "$WEB_ORIGIN/health"
wait_http "proxied api health after rollback" "$WEB_ORIGIN/api/health"
wait_http "direct api health after rollback" "http://127.0.0.1:$API_PORT/health"

echo "[rollback 5/5] 恢复部署前辅助文件"
for helper in app-config.js docker-compose.server.yml dc-openrag migrate-openrag; do
  key="$(printf '%s' "$helper" | tr '[:lower:].-' '[:upper:]__')_EXISTED"
  existed="${!key-0}"
  if [ "$existed" = "1" ] && [ -e "$BACKUP_DIR/$helper" ]; then
    cp -a "$BACKUP_DIR/$helper" "$SHARED_DIR/$helper"
  elif [ "$existed" = "0" ]; then
    rm -f "$SHARED_DIR/$helper"
  fi
done

cat >>"$REPORT_FILE" <<REPORT

## 回退结果

- Rolled back at UTC: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
- Previous release: $previous_release
- Selected services: $SELECTED_SERVICES
- Basic health after rollback: OK
REPORT
printf 'rolled_back_at=%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >"$BACKUP_DIR/rolled-back.ok"

echo "rollback-ok"
echo "previous_release=$previous_release"
