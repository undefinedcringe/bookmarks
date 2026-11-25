#!/usr/bin/env sh
# Restore a dump made by scripts/backup.sh, REPLACING the current bookmarks.
# Usage: scripts/restore.sh backups/bookmarks-YYYYMMDDTHHMMSSZ.dump [--yes]
set -eu

cd "$(dirname "$0")/.."

file="${1:-}"
if [ -z "$file" ] || [ ! -f "$file" ]; then
  echo "Usage: $0 <dump-file> [--yes]" >&2
  echo "Available backups:" >&2
  ls -1t backups/*.dump 2>/dev/null >&2 || echo "  (none in ./backups)" >&2
  exit 1
fi

if [ "${2:-}" != "--yes" ]; then
  printf 'This replaces ALL current bookmarks with the contents of %s. Continue? [y/N] ' "$file"
  read -r answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 1 ;; esac
fi

echo "Stopping app so nothing writes during the restore..."
docker compose stop app

docker compose up -d --wait db

echo "Restoring $file ..."
if ! docker compose exec -T db sh -c 'pg_restore --clean --if-exists --no-owner --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$file"; then
  # --single-transaction rolled everything back, so the old data is intact.
  echo "Restore FAILED; the database was left unchanged. Restarting app..." >&2
  docker compose up -d --wait app
  exit 1
fi

echo "Starting app..."
docker compose up -d --wait app
echo "Restore complete."
