#!/usr/bin/env sh
# Write a compressed, consistent dump of the bookmarks database to ./backups/.
# Usage: scripts/backup.sh [output-file]
# Safe to run while the app is up (pg_dump takes a consistent snapshot).
set -eu

cd "$(dirname "$0")/.."

mkdir -p backups
out="${1:-backups/bookmarks-$(date -u +%Y%m%dT%H%M%SZ).dump}"

# Variables are expanded inside the db container, so no secrets touch the host
# command line.
docker compose exec -T db sh -c 'pg_dump --format=custom --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$out.partial"
mv "$out.partial" "$out"

echo "Backup written to $out ($(wc -c < "$out" | tr -d ' ') bytes)"
