#!/usr/bin/env bash
set -euo pipefail

database="${PARTSOUQ_DATABASE:-output/partsouq-live.sqlite3}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
partsouq-crawler db-backup \
    --sqlite "$database" \
    --output "output/partsouq-backup-${timestamp}.sqlite3"
