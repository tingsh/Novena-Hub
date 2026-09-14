#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="${NOVENA_HUB_ROOT:-/home/shouheng/Novena-Platform/Novena-Hub}"
PYTHON="${NOVENA_HUB_PYTHON:-/home/shouheng/.venvs/novena/bin/python}"
MQTT_HOST=""
MQTT_PORT="1883"
KEY_ID="local-replay-2026-08"
SKIP_START="0"
SKIP_PREPARE="0"
PUBLIC_KEY_FILE="/tmp/novena-replay-gateway.env"

usage() {
  cat <<'USAGE_EOF'
Usage:
  bash scripts/hardware-test/prepare_laptop1_hub.sh --mqtt-host <laptop-1-lan-ip>

Options:
  --mqtt-host <ip-or-host>   Laptop 1 address reachable from the Pi CM4.
  --mqtt-port <port>         MQTT port for the local test. Must be 1883. Default: 1883.
  --key-id <id>              Guided Setup signing key id. Default: local-replay-2026-08.
  --skip-start               Update config and keys, but do not start local services.
  --skip-prepare             Do not run pilot_readiness_audit prepare.
  -h, --help                 Show this help.

This script prepares Laptop 1 as the local Hub machine for the path:
Laptop 2 Modbus simulator > Pi CM4 Gateway > Laptop 1 MQTT:1883 > local Novena Hub.
USAGE_EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mqtt-host)
      MQTT_HOST="${2:-}"
      shift 2
      ;;
    --mqtt-port)
      MQTT_PORT="${2:-}"
      shift 2
      ;;
    --key-id)
      KEY_ID="${2:-}"
      shift 2
      ;;
    --skip-start)
      SKIP_START="1"
      shift
      ;;
    --skip-prepare)
      SKIP_PREPARE="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$MQTT_HOST" ]]; then
  echo "Missing --mqtt-host. Use the Laptop 1 LAN IP reachable by the Pi." >&2
  exit 2
fi

if [[ "$MQTT_PORT" != "1883" ]]; then
  echo "This hardware replay guide is intentionally fixed to MQTT port 1883; got $MQTT_PORT." >&2
  exit 2
fi

if [[ ! -d "$ROOT" ]]; then
  echo "Hub repo not found at $ROOT. Set NOVENA_HUB_ROOT if your checkout is elsewhere." >&2
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Hub Python not found at $PYTHON. Set NOVENA_HUB_PYTHON if your venv is elsewhere." >&2
  exit 1
fi

cd "$ROOT"

export NOVENA_REPLAY_MQTT_HOST="$MQTT_HOST"
export NOVENA_REPLAY_MQTT_PORT="$MQTT_PORT"
export NOVENA_REPLAY_KEY_ID="$KEY_ID"
export NOVENA_REPLAY_PUBLIC_KEY_FILE="$PUBLIC_KEY_FILE"

"$PYTHON" - <<'INNER_PY'
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ENV_PATH = Path(".env")
MQTT_HOST = os.environ["NOVENA_REPLAY_MQTT_HOST"]
MQTT_PORT = os.environ["NOVENA_REPLAY_MQTT_PORT"]
DEFAULT_KEY_ID = os.environ["NOVENA_REPLAY_KEY_ID"]
PUBLIC_KEY_FILE = Path(os.environ["NOVENA_REPLAY_PUBLIC_KEY_FILE"])
GATEWAY_SERIAL = "NOV-AUDIT-FACTORY-HW"


def parse_env(lines):
    values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def update_env(lines, updates):
    out = []
    written = set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in written:
            out.append(f"{key}={value}")
    return "\n".join(out).rstrip() + "\n"


def load_private_key(values, key_id):
    seed = ""
    raw_keys = values.get("REMOTE_CONTROL_SIGNING_KEYS") or "{}"
    try:
        signing_keys = json.loads(raw_keys)
        if isinstance(signing_keys, dict):
            seed = signing_keys.get(key_id, "")
    except json.JSONDecodeError:
        seed = ""

    seed = seed or values.get("REMOTE_CONTROL_SIGNING_PRIVATE_KEY", "")
    if seed:
        private_raw = base64.b64decode(seed, validate=True)
        if len(private_raw) != 32:
            raise SystemExit("REMOTE_CONTROL signing seed must decode to 32 bytes.")
        return seed, Ed25519PrivateKey.from_private_bytes(private_raw)

    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(private_raw).decode(), private_key


lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
values = parse_env(lines)
key_id = values.get("REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID") or DEFAULT_KEY_ID
seed, private_key = load_private_key(values, key_id)
public_raw = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
public_b64 = base64.b64encode(public_raw).decode()

updates = {
    "MQTT_BROKER_HOST": "localhost",
    "MQTT_BROKER_PORT": "1883",
    "PUBLIC_MQTT_BROKER_SCHEME": "mqtt",
    "PUBLIC_MQTT_BROKER_HOST": MQTT_HOST,
    "PUBLIC_MQTT_BROKER_PORT": MQTT_PORT,
    "MQTT_ACCEPT_LEGACY_SHARED_INBOUND": "False",
    "REMOTE_CONTROL_ACTIVE_SIGNING_KEY_ID": key_id,
    "REMOTE_CONTROL_SIGNING_KEYS": json.dumps({key_id: seed}, separators=(",", ":")),
    "REMOTE_CONTROL_SIGNING_PRIVATE_KEY": seed,
}
ENV_PATH.write_text(update_env(lines, updates))

# Load the just-updated local settings so the printed replay claim code always
# follows the configured GATEWAY_CLAIM_SECRET instead of a stale documented value.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "novena_hub.settings")
import django

django.setup()
from apps.devices.services import compute_claim_code

claim_code = compute_claim_code(GATEWAY_SERIAL)
PUBLIC_KEY_FILE.write_text(
    f"GATEWAY_SERIAL={GATEWAY_SERIAL}\n"
    f"GATEWAY_CLAIM_CODE={claim_code}\n"
    f"GATEWAY_CONFIG_KEY_ID={key_id}\n"
    f"GATEWAY_CONFIG_PUBLIC_KEY_B64={public_b64}\n"
)
PUBLIC_KEY_FILE.chmod(0o600)

print("Updated .env for local hardware replay.")
print(f"Replay values written to {PUBLIC_KEY_FILE}")
INNER_PY

echo
echo "Gateway-facing replay values:"
cat "$PUBLIC_KEY_FILE"
echo

if [[ "$SKIP_START" != "1" ]]; then
  echo "Starting local Novena Hub services..."
  bash .agents/skills/novena-local-dev/scripts/start-novena-local-dev.sh
  echo
  echo "Running local service health check..."
  bash .agents/skills/novena-local-dev/scripts/health-check.sh
fi

if [[ "$SKIP_PREPARE" != "1" ]]; then
  echo
  echo "Preparing pilot audit login and hardware replay gateway inventory..."
  "$PYTHON" manage.py pilot_readiness_audit prepare
fi

echo
if command -v ss >/dev/null 2>&1; then
  echo "MQTT listener check for port 1883:"
  ss -ltnp | grep ':1883' || true
  if ss -ltn | grep -qE '(^|[[:space:]])0\.0\.0\.0:1883[[:space:]]'; then
    echo "OK: MQTT is listening on 0.0.0.0:1883 for the Pi."
  else
    echo "WARN: MQTT is not shown on 0.0.0.0:1883. Check mosquitto/wsl-lan-test.conf or firewall before using the Pi."
  fi
else
  echo "ss command not found; skipped MQTT listener check."
fi

echo
echo "Laptop 1 is prepared for hardware replay."
echo "Hub: http://localhost:8000/"
echo "Onboarding entry: http://localhost:8000/a/pilot-factory-energy/onboarding/"
echo "Replay values: $PUBLIC_KEY_FILE"
