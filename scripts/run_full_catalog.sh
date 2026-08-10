#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PARTSOUQ_USER_AGENT:-}" ]]; then
    echo "請先設定 PARTSOUQ_USER_AGENT（請包含有效聯絡方式）。" >&2
    exit 2
fi

transport_args=(--transport "${PARTSOUQ_TRANSPORT:-http}")
if [[ -n "${PARTSOUQ_BROWSER_EXECUTABLE:-}" ]]; then
    transport_args+=(--browser-executable "$PARTSOUQ_BROWSER_EXECUTABLE")
fi
if [[ "${PARTSOUQ_BROWSER_HEADLESS:-0}" == "1" ]]; then
    transport_args+=(--browser-headless)
fi

exec partsouq-crawler crawl-all \
    --run-id partsouq-genuine-full \
    --seed-url 'https://partsouq.com/en/catalog/genuine' \
    --max-pages 0 \
    --max-depth 0 \
    --concurrency "${PARTSOUQ_CONCURRENCY:-1}" \
    --delay "${PARTSOUQ_DELAY_SECONDS:-30}" \
    --timeout "${PARTSOUQ_REQUEST_TIMEOUT_SECONDS:-30}" \
    --retry-count "${PARTSOUQ_MAX_RETRIES:-1}" \
    --robots-policy require \
    --user-agent "$PARTSOUQ_USER_AGENT" \
    "${transport_args[@]}"
