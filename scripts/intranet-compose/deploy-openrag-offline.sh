#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

LOCK_DIR="$SHARED_DIR/.openrag-deploy.lock"
if [ ! -d "$SHARED_DIR" ]; then
  echo "缺少 shared 目录；请先准备 $SHARED_DIR/openrag.env" >&2
  exit 1
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "已有部署任务占用锁: $LOCK_DIR" >&2
  exit 1
fi
cleanup_lock() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT

rollback_armed=0
on_error() {
  exit_code=$?
  trap - ERR
  if [ "$rollback_armed" = "1" ]; then
    if [ "$REQUIRES_MIGRATION" = "0" ]; then
      echo "部署或验证失败，开始自动回退应用版本。" >&2
      SERVER_HOME="$SERVER_HOME" API_PORT="$API_PORT" WEB_PORT="$WEB_PORT" \
        bash "$SCRIPT_DIR/90-rollback.sh" || \
        echo "自动回退也失败，请查看 $CHECK_LOG 并人工处理。" >&2
    else
      echo "部署失败且发布包含数据库迁移，未自动回退；请依据迁移备份人工评估。" >&2
    fi
  fi
  exit "$exit_code"
}
trap on_error ERR

SERVER_HOME="$SERVER_HOME" API_PORT="$API_PORT" WEB_PORT="$WEB_PORT" \
  bash "$SCRIPT_DIR/00-preflight.sh"
SERVER_HOME="$SERVER_HOME" API_PORT="$API_PORT" WEB_PORT="$WEB_PORT" \
  bash "$SCRIPT_DIR/10-prepare.sh"
rollback_armed=1
SERVER_HOME="$SERVER_HOME" API_PORT="$API_PORT" WEB_PORT="$WEB_PORT" \
  bash "$SCRIPT_DIR/20-deploy.sh"
SERVER_HOME="$SERVER_HOME" API_PORT="$API_PORT" WEB_PORT="$WEB_PORT" \
  bash "$SCRIPT_DIR/30-verify.sh"
rollback_armed=0
trap - ERR

echo "offline-deploy-ok"
echo "report=$REPORT_FILE"
