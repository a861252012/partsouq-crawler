#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PARTSOUQ_USER_AGENT:-}" ]]; then
    echo "請先設定 PARTSOUQ_USER_AGENT（請包含有效聯絡方式）。" >&2
    exit 2
fi

exec partsouq-crawler crawl-all \
    --run-id partsouq-genuine-full \
    --seed-url 'https://partsouq.com/en/catalog/genuine' \
    --sqlite "${PARTSOUQ_DATABASE:-output/partsouq-live.sqlite3}" \
    --max-pages 0 \
    --max-depth 0 \
    --concurrency "${PARTSOUQ_CONCURRENCY:-1}" \
    --delay "${PARTSOUQ_DELAY_SECONDS:-5}" \
    --timeout "${PARTSOUQ_REQUEST_TIMEOUT_SECONDS:-30}" \
    --retry-count "${PARTSOUQ_MAX_RETRIES:-3}" \
    --robots-policy require \
    --user-agent "$PARTSOUQ_USER_AGENT"
