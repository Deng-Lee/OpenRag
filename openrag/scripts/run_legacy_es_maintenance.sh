#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  run_legacy_es_maintenance.sh \
    --server-home /path/to/OpenRag \
    --environment-label <label> \
    <audit-all|show|prepare-missing|apply-missing|prepare-orphans|\
delete-orphans|final-audit> [options]

The wrapper starts a one-shot API container, does not restart application services,
and stores reports under <server-home>/maintenance-reports/legacy-es.
EOF
}

server_home=""
environment_label=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-home)
      server_home="${2:-}"
      shift 2
      ;;
    --environment-label)
      environment_label="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

if [[ -z "$server_home" || -z "$environment_label" || $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

server_home="$(cd "$server_home" && pwd -P)"
dc_openrag="$server_home/shared/dc-openrag"
report_root="$server_home/maintenance-reports"
if [[ ! -x "$dc_openrag" ]]; then
  echo "Compose helper is not executable: $dc_openrag" >&2
  exit 1
fi

mkdir -p "$report_root/legacy-es"
chmod 700 "$report_root" "$report_root/legacy-es"

exec "$dc_openrag" run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$report_root:/reports" \
  api \
  python /app/scripts/legacy_es_maintenance.py \
  --environment-label "$environment_label" \
  --output-root /reports/legacy-es \
  "$@"
