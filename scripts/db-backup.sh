#!/usr/bin/env bash
# db-backup.sh -- Database backup and restore utilities for OpenCPO
set -euo pipefail

ACTION="${1:-backup}"
BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
mkdir -p "$BACKUP_DIR"

# Read DB config from env (matching what config.py expects)
PG_HOST="${PG_HOST:-postgres}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-ocpp}"
PG_PASSWORD="${PG_PASSWORD:-}"
PG_NAME="${PG_NAME:-ocpp}"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
VERSION_FILE="${VERSION_FILE:-/app/opencpo/version.txt}"
VERSION="$(cat "$VERSION_FILE" 2>/dev/null || echo 'unknown')"

case "$ACTION" in
  backup)
    FILENAME="opencpo-${VERSION}-${TIMESTAMP}.dump"
    echo "Backing up database to ${BACKUP_DIR}/${FILENAME}..."
    PGPASSWORD="$PG_PASSWORD" pg_dump \
      -h "$PG_HOST" -p "$PG_PORT" \
      -U "$PG_USER" -d "$PG_NAME" \
      --format=custom \
      -f "${BACKUP_DIR}/${FILENAME}"
    echo "$FILENAME"
    ;;
  restore)
    FILENAME="${2:-}"
    if [ -z "$FILENAME" ]; then
      echo "Usage: $0 restore <filename>"
      echo "Available backups:"
      ls -1 "$BACKUP_DIR"/*.dump 2>/dev/null || echo "No backups found"
      exit 1
    fi
    FILEPATH="${BACKUP_DIR}/${FILENAME}"
    if [ ! -f "$FILEPATH" ]; then
      echo "Backup not found: $FILEPATH"
      exit 1
    fi
    echo "Restoring from ${FILEPATH}..."
    echo "WARNING: This will drop the current database. Continue? (y/N)"
    # Non-interactive mode: just print what would happen
    if [ "${AUTO_CONFIRM:-}" = "1" ]; then
      PGPASSWORD="$PG_PASSWORD" pg_restore \
        -h "$PG_HOST" -p "$PG_PORT" \
        -U "$PG_USER" -d "$PG_NAME" \
        --clean --if-exists \
        "$FILEPATH"
      echo "Restore complete."
    else
      echo "(dry run -- use AUTO_CONFIRM=1 to execute)"
      pg_restore --list "$FILEPATH" | head -20
    fi
    ;;
  list)
    echo "Available backups in ${BACKUP_DIR}:"
    ls -lh "$BACKUP_DIR"/*.dump 2>/dev/null || echo "No backups found"
    ;;
  *)
    echo "Usage: $0 {backup|restore|list} [filename]"
    exit 1
    ;;
esac
