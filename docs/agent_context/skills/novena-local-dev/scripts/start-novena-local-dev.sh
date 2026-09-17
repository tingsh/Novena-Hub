#!/usr/bin/env bash
set -euo pipefail

ROOT="${NOVENA_HUB_ROOT:-/home/shouheng/Projects/Novena-Platform/Novena-Hub}"
VENV="${NOVENA_PYTHON_VENV:-/home/shouheng/.venvs/novena}"
PYTHON="$VENV/bin/python"

cd "$ROOT"

info() {
  printf '[novena-local-dev] %s\n' "$*"
}

fail() {
  printf '[novena-local-dev] ERROR: %s\n' "$*" >&2
  exit 1
}

need_file() {
  [[ -e "$1" ]] || fail "Missing required path: $1"
}

tcp_ready() {
  local host="$1"
  local port="$2"
  timeout 2 bash -c "cat < /dev/null > /dev/tcp/$host/$port" >/dev/null 2>&1
}

start_local_service() {
  local service_name="$1"
  if command -v service >/dev/null 2>&1; then
    if sudo -n service "$service_name" start >/dev/null 2>&1; then
      return 0
    fi
  fi
  if [[ -x "/etc/init.d/$service_name" ]]; then
    if sudo -n "/etc/init.d/$service_name" start >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

need_file "$PYTHON"
need_file "$ROOT/manage.py"
need_file "$ROOT/scripts/start_wsl_dev_stack.sh"

info "Using repo: $ROOT"
info "Using Python: $PYTHON"

if command -v pg_isready >/dev/null 2>&1 && pg_isready -h localhost -p 5432 >/dev/null 2>&1; then
  info "PostgreSQL is already ready on localhost:5432"
else
  info "Starting PostgreSQL service on Ubuntu"
  start_local_service postgresql || fail "PostgreSQL is not ready and could not be started non-interactively. Run: sudo service postgresql start"
fi

if command -v redis-cli >/dev/null 2>&1 && redis-cli -h localhost -p 6379 ping >/dev/null 2>&1; then
  info "Redis is already ready on localhost:6379"
else
  info "Starting Redis service on Ubuntu"
  start_local_service redis-server || fail "Redis is not ready and could not be started non-interactively. Run: sudo service redis-server start"
fi

info "Checking Django configuration"
"$PYTHON" manage.py check

info "Checking database migrations"
if ! "$PYTHON" manage.py migrate --check; then
  fail "Database has pending migrations. Run: $PYTHON manage.py migrate"
fi

info "Checking TimescaleDB hypertable state"
"$PYTHON" manage.py verify_timescale
"$PYTHON" manage.py dbshell -- -X -v ON_ERROR_STOP=1 -f "$ROOT/scripts/database/verify_timescale_policies.sql"

info "Starting Mosquitto, Django, Celery, MQTT consumer, and Vite via repo script"
bash "$ROOT/scripts/start_wsl_dev_stack.sh"

info "Startup commands issued. Run health-check.sh to confirm readiness."
