#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
core_python="${PARTSOUQ_CORE_PYTHON:-$project_root/.venv/bin/python}"

required_variables=(
    PARTSOUQ_MYSQL_HOST
    PARTSOUQ_MYSQL_DATABASE
    PARTSOUQ_MYSQL_USER
    PARTSOUQ_MYSQL_PASSWORD
    PARTSOUQ_USER_AGENT
    NHTSA_MYSQL_HOST
    NHTSA_MYSQL_DATABASE
    NHTSA_MYSQL_USER
    NHTSA_MYSQL_PASSWORD
    NHTSA_RAW_DIR
    NHTSA_USER_AGENT
)

for variable_name in "${required_variables[@]}"; do
    if [[ -z "${!variable_name:-}" ]]; then
        echo "missing required environment variable: $variable_name" >&2
        exit 1
    fi
done

if [[ "${PARTSOUQ_TRANSPORT:-browser}" != "browser" && "${PARTSOUQ_TRANSPORT:-browser}" != "http" ]]; then
    echo "PARTSOUQ_TRANSPORT must be browser or http for monthly sync" >&2
    exit 1
fi
if [[ "${PARTSOUQ_TRANSPORT:-browser}" == "browser" && "${PARTSOUQ_BROWSER_HEADLESS:-1}" != "1" ]]; then
    echo "PARTSOUQ_BROWSER_HEADLESS must be 1 for unattended monthly sync" >&2
    exit 1
fi
if [[ ! -x "$core_python" ]]; then
    echo "core Python is not executable: $core_python" >&2
    exit 1
fi
if [[ -n "${PARTSOUQ_BROWSER_EXECUTABLE:-}" && ! -x "$PARTSOUQ_BROWSER_EXECUTABLE" ]]; then
    echo "browser is not executable: $PARTSOUQ_BROWSER_EXECUTABLE" >&2
    exit 1
fi

mkdir -p "$NHTSA_RAW_DIR"

exec "$core_python" -m partsouq_crawler monthly-sync \
    --timezone "${MONTHLY_SYNC_TIMEZONE:-Asia/Taipei}" \
    --lease-seconds "${MONTHLY_SYNC_LEASE_SECONDS:-900}" \
    --heartbeat-seconds "${MONTHLY_SYNC_HEARTBEAT_SECONDS:-60}" \
    --max-attempts "${MONTHLY_SYNC_MAX_ATTEMPTS:-3}"
