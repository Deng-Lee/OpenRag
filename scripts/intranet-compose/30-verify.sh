#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

require_file "$BACKUP_DIR/applied.ok"
mkdir -p "$ARTIFACT_DIR"
: >"$CHECK_LOG"

diagnostics() {
  {
    echo "===== compose ps ====="
    dc ps || true
    echo "===== api logs ====="
    dc logs --tail=120 api || true
    echo "===== web logs ====="
    dc logs --tail=120 web || true
    echo "===== worker logs ====="
    dc logs --tail=120 task-worker || true
  } >>"$CHECK_LOG" 2>&1
  echo "验证失败，诊断日志: $CHECK_LOG" >&2
}
trap diagnostics ERR

echo "[verify 1/7] 校验激活版本和 Compose"
test -L "$CURRENT_LINK"
test "$(readlink -f "$CURRENT_LINK")" = "$(readlink -f "$RELEASE_DIR")"
dc config --quiet

echo "[verify 2/7] 等待核心容器进入 running"
core_containers=(
  openrag-web-prod openrag-api-prod openrag-task-worker openrag-postgres-prod
  openrag-milvus-prod openrag-milvus-etcd-prod openrag-milvus-minio-prod
  openrag-elasticsearch-prod
)
all_running=0
for _ in $(seq 1 30); do
  all_running=1
  for container in "${core_containers[@]}"; do
    status="$(docker container inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
    if [ "$status" != "running" ]; then
      all_running=0
      break
    fi
  done
  if [ "$all_running" = "1" ]; then
    break
  fi
  sleep 4
done
if [ "$all_running" != "1" ]; then
  echo "并非所有核心容器都处于 running。" >&2
  exit 1
fi
dc ps | tee -a "$CHECK_LOG"

echo "[verify 3/7] 校验容器确实使用本版本镜像"
for service in "${SELECTED_SERVICE_LIST[@]}"; do
  image="$(service_image "$service")"
  container="$(service_container "$service")"
  expected_id="$(docker image inspect --format '{{.Id}}' "$image:$DEPLOY_VERSION")"
  actual_id="$(docker container inspect --format '{{.Image}}' "$container")"
  if [ "$expected_id" != "$actual_id" ]; then
    echo "$service 容器没有运行本版本镜像: expected=$expected_id actual=$actual_id" >&2
    exit 1
  fi
  echo "OK: $service image=$expected_id"
done

echo "[verify 4/7] 校验 HTTP 链路"
wait_http "web health" "$WEB_ORIGIN/health"
wait_http "proxied api health" "$WEB_ORIGIN/api/health"
wait_http "direct api health" "http://127.0.0.1:$API_PORT/health"
wait_http "app config" "$WEB_ORIGIN/app-config.js"
curl -fsS "$WEB_ORIGIN/app-config.js" | grep -F 'apiBaseUrl: "/api"' >/dev/null

echo "[verify 5/7] 校验 Excel 专用运行时链路"
if [[ " $SELECTED_SERVICES " == *" task-worker "* ]]; then
  docker run --rm -i "openrag-task-worker:$DEPLOY_VERSION" \
    python - <"$SCRIPT_DIR/verify_excel_table_chunking.py" | tee -a "$CHECK_LOG"
else
  echo "本包未更新 task-worker，跳过 Excel worker 镜像冒烟测试。"
fi

echo "[verify 6/7] 校验数据库版本和关键日志"
docker exec -i openrag-postgres-prod psql -U openrag -d openrag \
  -c "select version_num from alembic_version;" | tee -a "$CHECK_LOG"
dc logs --tail=80 api web task-worker >>"$CHECK_LOG" 2>&1

echo "[verify 7/7] 完成部署报告"
cat >>"$REPORT_FILE" <<REPORT

## 自动验证

- Compose config: OK
- Core containers running: OK
- Selected image identity: OK
- Web health: OK
- Proxied API health: OK
- Direct API health: OK
- app-config.js: OK
- Excel table token smoke test: $(if [[ " $SELECTED_SERVICES " == *" task-worker "* ]]; then echo OK; else echo skipped; fi)
- Verified at UTC: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

## 人工验收

- 浏览器登录并打开目标 Workspace。
- 上传此前失败的大 Excel，确认任务成功、最大 Chunk 不超过配置值。
- 执行一次知识库检索，确认 Elasticsearch/Milvus 结果正常。

## 回退

- 应用回退脚本: scripts/90-rollback.sh
- 数据库迁移发布默认禁止自动回退；先评估 schema 兼容性和迁移前备份。
REPORT
printf 'verified_at=%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >"$BACKUP_DIR/verified.ok"
trap - ERR

echo "verification-ok"
echo "report=$REPORT_FILE"
