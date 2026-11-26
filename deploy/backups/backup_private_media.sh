#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="${NOVENA_ROOT:-/opt/novena/Novena-Hub}"
PRIVATE_MEDIA_DIR="${NOVENA_PRIVATE_MEDIA_DIR:-$ROOT_DIR/private_media}"
BACKUP_ROOT="${NOVENA_PRIVATE_MEDIA_BACKUP_DIR:-$ROOT_DIR/backups/private_media}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ "${NOVENA_OFFSITE_REQUIRED:-0}" == 1 && -z "${PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE:-}" ]]; then
  echo "PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE is required for this off-server backup job" >&2
  exit 1
fi

if [[ ! -d "$PRIVATE_MEDIA_DIR" ]]; then
  echo "Private media directory is missing: $PRIVATE_MEDIA_DIR" >&2
  exit 1
fi
install -d -m 0700 "$BACKUP_ROOT" "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly" "$BACKUP_ROOT/monthly"

BACKUP_FILE="$BACKUP_ROOT/daily/novena_private_media_${TIMESTAMP}.tar.gz"
TEMP_FILE="$(mktemp "$BACKUP_ROOT/daily/.novena_private_media.XXXXXXXX.tar.gz")"
trap 'rm -f "$TEMP_FILE"' EXIT

echo "Creating private media backup: $BACKUP_FILE"
tar --create --gzip --file "$TEMP_FILE" --directory "$PRIVATE_MEDIA_DIR" .
tar --list --gzip --file "$TEMP_FILE" > /dev/null
chmod 0600 "$TEMP_FILE"
mv "$TEMP_FILE" "$BACKUP_FILE"
trap - EXIT

if [[ "$(date -u +%u)" == "7" ]]; then
  cp "$BACKUP_FILE" "$BACKUP_ROOT/weekly/"
fi

if [[ "$(date -u +%d)" == "01" ]]; then
  cp "$BACKUP_FILE" "$BACKUP_ROOT/monthly/"
fi

if [[ -n "${PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE:-}" ]]; then
  remote_name="${PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE%%:*}"
  if [[ "$PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE" != *:* ]] || ! rclone config show "$remote_name" | grep -Eq '^type[[:space:]]*=[[:space:]]*crypt$'; then
    echo "PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE must name a configured rclone crypt remote; upload skipped" >&2
    exit 1
  fi
  echo "Uploading private media backup tree to: $PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE"
  rclone copy "$BACKUP_ROOT" "$PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE"
  rclone cryptcheck "$BACKUP_ROOT" "$PRIVATE_MEDIA_BACKUP_RCLONE_REMOTE"
fi

echo "Private media backup complete: $BACKUP_FILE"
