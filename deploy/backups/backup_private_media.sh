#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${NOVENA_ROOT:-/opt/novena/Novena-Hub}"
PRIVATE_MEDIA_DIR="${NOVENA_PRIVATE_MEDIA_DIR:-$ROOT_DIR/private_media}"
BACKUP_ROOT="${NOVENA_PRIVATE_MEDIA_BACKUP_DIR:-$ROOT_DIR/backups/private_media}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$PRIVATE_MEDIA_DIR" "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly" "$BACKUP_ROOT/monthly"

BACKUP_FILE="$BACKUP_ROOT/daily/novena_private_media_${TIMESTAMP}.tar.gz"
TEMP_FILE="${BACKUP_FILE}.partial"

echo "Creating private media backup: $BACKUP_FILE"
tar --create --gzip --file "$TEMP_FILE" --directory "$PRIVATE_MEDIA_DIR" .
mv "$TEMP_FILE" "$BACKUP_FILE"

if [[ "$(date -u +%u)" == "7" ]]; then
  cp "$BACKUP_FILE" "$BACKUP_ROOT/weekly/"
fi

if [[ "$(date -u +%d)" == "01" ]]; then
  cp "$BACKUP_FILE" "$BACKUP_ROOT/monthly/"
fi

if [[ -n "${PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE:-}" ]]; then
  echo "Uploading private media backup tree to: $PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE"
  rclone copy "$BACKUP_ROOT" "$PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE"
fi

echo "Private media backup complete: $BACKUP_FILE"
