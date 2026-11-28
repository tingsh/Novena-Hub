#!/usr/bin/env bash
set -euo pipefail

BACKUP_FILE="${BACKUP_FILE:-${1:-}}"
TARGET_DATABASE_URL="${RESTORE_DATABASE_URL:-}"

if [[ -z "$BACKUP_FILE" || ! -f "$BACKUP_FILE" ]]; then
  echo "Usage: BACKUP_FILE=/path/to/backup.dump.gz RESTORE_CONFIRM=restore-now $0" >&2
  echo "Or pass the backup path as the first argument." >&2
  exit 1
fi

if [[ -z "$TARGET_DATABASE_URL" ]]; then
  echo "RESTORE_DATABASE_URL is required; the restore target must be explicit." >&2
  exit 1
fi

if [[ "${RESTORE_CONFIRM:-}" != "restore-now" ]]; then
  echo "Refusing to restore without RESTORE_CONFIRM=restore-now." >&2
  echo "Restores can overwrite data. Test on a disposable database first." >&2
  exit 1
fi

psql_restore() {
  psql -X -v ON_ERROR_STOP=1 --dbname "$TARGET_DATABASE_URL" "$@"
}

# Restores require an empty target. In particular, never load a drill archive
# over the running Hub database just because its URL was present in an env file.
existing_tables="$(psql_restore -At -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [[ "$existing_tables" != "0" ]]; then
  echo "Restore target has public tables; create an empty database before restoring." >&2
  exit 1
fi

# TimescaleDB's bgw_job rows contain their owner as data. --no-owner does not
# remove that reference, so a fresh cluster needs the Novena role beforehand.
role_exists="$(psql_restore -At -c "SELECT 1 FROM pg_roles WHERE rolname = 'novena'")"
if [[ "$role_exists" != "1" ]]; then
  psql_restore -c 'CREATE ROLE novena LOGIN;'
fi

psql_restore -c 'CREATE EXTENSION IF NOT EXISTS timescaledb;'
restore_prepared=0
finish_restore() {
  status=$?
  trap - EXIT
  if [[ "$restore_prepared" == "1" ]]; then
    if ! psql_restore -c 'SELECT timescaledb_post_restore();'; then
      echo "TimescaleDB post-restore cleanup failed; the target needs operator attention." >&2
      exit 1
    fi
  fi
  exit "$status"
}
trap finish_restore EXIT

psql_restore -c 'SELECT timescaledb_pre_restore();'
restore_prepared=1
echo "Restoring $BACKUP_FILE into the empty target database."
gzip -dc "$BACKUP_FILE" | pg_restore --exit-on-error --no-owner --no-acl --dbname "$TARGET_DATABASE_URL"
restore_prepared=0
psql_restore -c 'SELECT timescaledb_post_restore();'
echo "Restore complete."
