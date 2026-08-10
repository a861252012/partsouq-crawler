#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
core_python="${PARTSOUQ_CORE_PYTHON:-$project_root/.venv/bin/python}"
xvfb_run="${PARTSOUQ_XVFB_RUN:-/usr/bin/xvfb-run}"

required_variables=(
    PARTSOUQ_MYSQL_HOST
    PARTSOUQ_MYSQL_DATABASE
    PARTSOUQ_MYSQL_USER
    PARTSOUQ_MYSQL_PASSWORD
    PARTSOUQ_BROWSER_EXECUTABLE
    PARTSOUQ_BROWSER_PROFILE_DIR
    PARTSOUQ_BROWSER_WORKER_COMMAND
    NHTSA_MYSQL_HOST
    NHTSA_MYSQL_DATABASE
    NHTSA_MYSQL_USER
    NHTSA_MYSQL_PASSWORD
    NHTSA_RAW_DIR
)

for variable_name in "${required_variables[@]}"; do
    if [[ -z "${!variable_name:-}" ]]; then
        echo "missing required environment variable: $variable_name" >&2
        exit 1
    fi
done

if [[ "${PARTSOUQ_BROWSER_HEADLESS:-0}" != "0" ]]; then
    echo "PARTSOUQ_BROWSER_HEADLESS must be 0; use Xvfb for an unattended display" >&2
    exit 1
fi
if [[ "${PARTSOUQ_BROWSER_SANDBOX:-1}" != "1" ]]; then
    echo "PARTSOUQ_BROWSER_SANDBOX must be 1 for the supported host deployment" >&2
    exit 1
fi
if [[ ! -x "$core_python" ]]; then
    echo "core Python is not executable: $core_python" >&2
    exit 1
fi
if [[ ! -x "$xvfb_run" ]]; then
    echo "xvfb-run is not executable: $xvfb_run" >&2
    exit 1
fi
if [[ ! -x "$PARTSOUQ_BROWSER_EXECUTABLE" ]]; then
    echo "browser is not executable: $PARTSOUQ_BROWSER_EXECUTABLE" >&2
    exit 1
fi

mkdir -p "$PARTSOUQ_BROWSER_PROFILE_DIR" "$NHTSA_RAW_DIR"

exec "$xvfb_run" \
    --auto-servernum \
    --server-args="-screen 0 1920x1080x24 -nolisten tcp" \
    "$core_python" -m partsouq_crawler monthly-sync \
    --timezone "${MONTHLY_SYNC_TIMEZONE:-Asia/Taipei}" \
    --lease-seconds "${MONTHLY_SYNC_LEASE_SECONDS:-900}" \
    --heartbeat-seconds "${MONTHLY_SYNC_HEARTBEAT_SECONDS:-60}" \
    --max-attempts "${MONTHLY_SYNC_MAX_ATTEMPTS:-3}"
