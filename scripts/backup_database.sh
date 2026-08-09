#!/usr/bin/env bash
set -euo pipefail

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="output/partsouq-backup-${timestamp}.sql"
container="${PARTSOUQ_MYSQL_CONTAINER:-nhtsa-mysql}"
database="${PARTSOUQ_MYSQL_DATABASE:-partsouq}"
user="${PARTSOUQ_MYSQL_USER:-partsouq}"
password="${PARTSOUQ_MYSQL_PASSWORD:-partsouq-local}"

mkdir -p output
docker exec -e MYSQL_PWD="$password" "$container" mysqldump \
    --user="$user" \
    --single-transaction \
    --quick \
    --hex-blob \
    --no-tablespaces \
    "$database" > "$output"
shasum -a 256 "$output" > "${output}.sha256"
echo "$output"
