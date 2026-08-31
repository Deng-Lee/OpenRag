#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

require_file "$BACKUP_DIR/prepared.ok"
require_file "$STATE_FILE"
if [ -e "$BACKUP_DIR/applied.ok" ]; then
  echo "该版本已经应用，拒绝重复执行 deploy: $DEPLOY_VERSION" >&2
  exit 1
fi

echo "[deploy 1/6] 激活新源码发布"
if [ -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
  legacy_backup="$BACKUP_DIR/current-before-deploy"
  if [ -e "$legacy_backup" ]; then
    echo "旧 current 备份目标已存在: $legacy_backup" >&2
    exit 1
  fi
  mv "$CURRENT_LINK" "$legacy_backup"
  write_state "PREVIOUS_RELEASE" "$legacy_backup"
fi
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
test "$(readlink -f "$CURRENT_LINK")" = "$(readlink -f "$RELEASE_DIR")"

echo "[deploy 2/6] 写入服务器 Compose 辅助文件"
write_shared_helpers
dc config --quiet

echo "[deploy 3/6] 确认持久化数据卷策略"
if [ -s "$SHARED_DIR/docker-compose.data-active.yml" ]; then
  echo "使用现有数据卷覆盖文件: $SHARED_DIR/docker-compose.data-active.yml"
  dc config >/dev/null
else
  echo "未发现 data-active 覆盖文件，沿用基础 Compose 中的既有命名卷。"
fi

echo "[deploy 4/6] 按风险执行数据库迁移"
if [ "$REQUIRES_MIGRATION" = "1" ]; then
  command -v gzip >/dev/null
  dc stop api task-worker || true
  dc up -d --no-build postgres
  for _ in $(seq 1 30); do
    if docker exec openrag-postgres-prod pg_isready -U openrag -d openrag >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  docker exec openrag-postgres-prod pg_isready -U openrag -d openrag >/dev/null
  db_backup="$BACKUP_DIR/postgres-before-migration.sql.gz"
  docker exec openrag-postgres-prod pg_dump -U openrag -d openrag | gzip -c >"$db_backup"
  test -s "$db_backup"
  # 在执行迁移前标记，任何中途失败都禁止脚本自动假定数据库可降级。
  write_state "MIGRATION_RAN" "1"
  "$SHARED_DIR/migrate-openrag"
  echo "数据库迁移完成，迁移前备份: $db_backup"
else
  echo "本发布不包含数据库迁移。"
fi

echo "[deploy 5/6] 仅重建受影响应用服务"
initial_deploy=0
if ! docker container inspect openrag-postgres-prod >/dev/null 2>&1; then
  initial_deploy=1
fi
if [ "$initial_deploy" = "1" ]; then
  echo "未发现现有 OpenRag 数据库容器，按首次部署启动全部服务。"
  dc up -d --no-build
else
  dc up -d --no-build --no-deps --force-recreate "${SELECTED_SERVICE_LIST[@]}"
fi

echo "[deploy 6/6] 保存应用状态"
printf 'applied_at=%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >"$BACKUP_DIR/applied.ok"
cat >"$REPORT_FILE" <<REPORT
# OpenRag 内网离线部署报告

- Version: $DEPLOY_VERSION
- Git commit: $GIT_COMMIT
- Applied at UTC: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
- Server home: $SERVER_HOME
- Selected services: $SELECTED_SERVICES
- Requires migration: $REQUIRES_MIGRATION
- Data override preserved: $(if [ -s "$SHARED_DIR/docker-compose.data-active.yml" ]; then echo yes; else echo no; fi)

## 状态

- 源码和镜像已经应用。
- 尚需执行 30-verify.sh；只有验证成功才算部署完成。
REPORT

echo "deploy-applied"
echo "下一步: SERVER_HOME='$SERVER_HOME' bash '$SCRIPT_DIR/30-verify.sh'"
