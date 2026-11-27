#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="${NOVENA_ROOT:-/opt/novena/Novena-Hub}"
ENV_FILE="${NOVENA_ENV_FILE:-$ROOT_DIR/deploy/env/production.env}"
BACKUP_ROOT="${NOVENA_BACKUP_DIR:-$ROOT_DIR/backups/postgres}"
DB_MODE="${NOVENA_BACKUP_DB_MODE:-direct}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DB_URL="${DATABASE_URL:-}"

if [[ "${NOVENA_OFFSITE_REQUIRED:-0}" == 1 && -z "${BACKUP_RCLONE_REMOTE:-}" ]]; then
  echo "BACKUP_RCLONE_REMOTE is required for this off-server backup job" >&2
  exit 1
fi

if [[ "$DB_MODE" == direct && -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  DB_URL="${DATABASE_URL:-$DB_URL}"
fi

install -d -m 0700 "$BACKUP_ROOT" "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly" "$BACKUP_ROOT/monthly"

BACKUP_FILE="$BACKUP_ROOT/daily/novena_hub_${TIMESTAMP}.dump.gz"
TEMP_FILE="$(mktemp "$BACKUP_ROOT/daily/.novena_hub.XXXXXXXX.dump.gz")"
trap 'rm -f "$TEMP_FILE"' EXIT

echo "Creating Postgres backup: $BACKUP_FILE"
case "$DB_MODE" in
  compose)
    docker compose -f "$ROOT_DIR/docker-compose.prod.yml" exec -T postgres \
      sh -c 'exec pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
      | gzip -9 > "$TEMP_FILE"
    ;;
  direct)
    if [[ -z "$DB_URL" ]]; then
      echo "DATABASE_URL is required in direct mode. Set it in the environment or $ENV_FILE." >&2
      exit 1
    fi
    pg_dump --format=custom --no-owner --no-acl "$DB_URL" | gzip -9 > "$TEMP_FILE"
    ;;
  *)
    echo "NOVENA_BACKUP_DB_MODE must be direct or compose." >&2
    exit 1
    ;;
esac

test -s "$TEMP_FILE"
gzip -t "$TEMP_FILE"
if [[ "$DB_MODE" == compose ]]; then
  gzip -dc "$TEMP_FILE" | docker compose -f "$ROOT_DIR/docker-compose.prod.yml" exec -T postgres pg_restore --list > /dev/null
else
  gzip -dc "$TEMP_FILE" | pg_restore --list > /dev/null
fi
chmod 0600 "$TEMP_FILE"
mv "$TEMP_FILE" "$BACKUP_FILE"
trap - EXIT

if [[ "$(date -u +%u)" == "7" ]]; then
  cp "$BACKUP_FILE" "$BACKUP_ROOT/weekly/"
fi

if [[ "$(date -u +%d)" == "01" ]]; then
  cp "$BACKUP_FILE" "$BACKUP_ROOT/monthly/"
fi

if [[ -n "${BACKUP_RCLONE_REMOTE:-}" ]]; then
  remote_name="${BACKUP_RCLONE_REMOTE%%:*}"
  if [[ "$BACKUP_RCLONE_REMOTE" != *:* ]] || ! rclone config show "$remote_name" | grep -Eq '^type[[:space:]]*=[[:space:]]*crypt$'; then
    echo "BACKUP_RCLONE_REMOTE must name a configured rclone crypt remote; upload skipped" >&2
    exit 1
  fi
  echo "Uploading backup tree to rclone remote: $BACKUP_RCLONE_REMOTE"
  rclone copy "$BACKUP_ROOT" "$BACKUP_RCLONE_REMOTE"
  rclone cryptcheck "$BACKUP_ROOT" "$BACKUP_RCLONE_REMOTE" --one-way
fi

echo "Backup complete: $BACKUP_FILE"
