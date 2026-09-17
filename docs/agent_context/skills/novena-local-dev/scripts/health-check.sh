#!/usr/bin/env bash
set -euo pipefail

ROOT="${NOVENA_HUB_ROOT:-/home/shouheng/Projects/Novena-Platform/Novena-Hub}"
VENV="${NOVENA_PYTHON_VENV:-/home/shouheng/.venvs/novena}"
PYTHON="$VENV/bin/python"

cd "$ROOT"

ok() {
  printf '[ok] %s\n' "$*"
}

warn() {
  printf '[warn] %s\n' "$*" >&2
}

check_pid() {
  local name="$1"
  local file="$ROOT/.dev-pids/$name.pid"
  if [[ -f "$file" ]] && kill -0 "$(cat "$file")" 2>/dev/null; then
    ok "$name running as pid $(cat "$file")"
  else
    warn "$name is not running according to $file"
  fi
}

check_http() {
  local name="$1"
  local url="$2"
  if curl -fsS --max-time 5 "$url" >/dev/null; then
    ok "$name responds at $url"
  else
    warn "$name did not respond at $url"
  fi
}

check_tcp() {
  local name="$1"
  local host="$2"
  local port="$3"
  if timeout 2 bash -c "cat < /dev/null > /dev/tcp/$host/$port" >/dev/null 2>&1; then
    ok "$name accepts TCP on $host:$port"
  else
    warn "$name is not accepting TCP on $host:$port"
  fi
}

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet mosquitto; then
  ok "Mosquitto system service is running"
else
  check_pid mosquitto-wsl
fi
check_pid django-wsl
check_pid celery-wsl
check_pid mqtt-consumer-wsl
check_pid vite-wsl

check_tcp PostgreSQL localhost 5432
check_tcp Redis localhost 6379
check_tcp Mosquitto localhost 1883
check_http Django http://127.0.0.1:8000/
check_http Vite http://127.0.0.1:5173/

if command -v redis-cli >/dev/null 2>&1 && redis-cli -h localhost -p 6379 ping | grep -q PONG; then
  ok "Redis ping returned PONG"
else
  warn "Redis ping failed"
fi

if command -v pg_isready >/dev/null 2>&1 && pg_isready -h localhost -p 5432 >/dev/null 2>&1; then
  ok "PostgreSQL readiness check passed"
else
  warn "PostgreSQL readiness check failed"
fi

"$PYTHON" manage.py migrate --check
"$PYTHON" manage.py verify_timescale
"$PYTHON" manage.py dbshell -- -X -v ON_ERROR_STOP=1 -f "$ROOT/scripts/database/verify_timescale_policies.sql"
