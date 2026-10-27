#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="${NOVENA_ROOT:-/opt/novena/Novena-Hub}"
SOURCE_FILE="${MQTT_DYNSEC_SOURCE:-$ROOT_DIR/deploy/mosquitto/data/dynamic-security.json}"
BACKUP_ROOT="${MQTT_DYNSEC_BACKUP_DIR:-/var/backups/novena/mqtt-dynsec}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! -f "$SOURCE_FILE" || ! -r "$SOURCE_FILE" ]]; then
  echo "Mosquitto Dynamic Security state is missing or unreadable: $SOURCE_FILE" >&2
  exit 1
fi

install -d -m 0700 "$BACKUP_ROOT" "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly" "$BACKUP_ROOT/monthly"
BACKUP_FILE="$BACKUP_ROOT/daily/dynamic-security_${TIMESTAMP}.json"
TEMP_FILE="$(mktemp "$BACKUP_ROOT/daily/.dynamic-security.XXXXXXXX")"
trap 'rm -f "$TEMP_FILE"' EXIT

# Copy and validate the saved bytes. A concurrent broker write must never leave
# an invalid snapshot that silently replaces the last good backup.
valid=0
for attempt in 1 2 3; do
  cp "$SOURCE_FILE" "$TEMP_FILE"
  if python3 - "$TEMP_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as state_file:
        state = json.load(state_file)
    if not isinstance(state, dict) or not isinstance(state.get("clients"), list) or not isinstance(state.get("roles"), list):
        raise ValueError("missing clients or roles list")
except (OSError, UnicodeError, ValueError) as exc:
    print(f"Invalid Mosquitto Dynamic Security snapshot: {exc}", file=sys.stderr)
    sys.exit(1)
PY
  then
    valid=1
    break
  fi
  sleep 1
done
if [[ "$valid" != 1 ]]; then
  echo "Mosquitto Dynamic Security state failed validation; no backup published" >&2
  exit 1
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

find "$BACKUP_ROOT/daily" -type f -name '*.json' -mtime +7 -delete
find "$BACKUP_ROOT/weekly" -type f -name '*.json' -mtime +35 -delete
find "$BACKUP_ROOT/monthly" -type f -name '*.json' -mtime +100 -delete

if [[ -n "${MQTT_DYNSEC_BACKUP_RCLONE_REMOTE:-}" ]]; then
  rclone copy "$BACKUP_ROOT" "$MQTT_DYNSEC_BACKUP_RCLONE_REMOTE"
fi

echo "Mosquitto Dynamic Security backup complete: $BACKUP_FILE"
