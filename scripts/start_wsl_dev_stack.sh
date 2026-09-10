#!/usr/bin/env bash
set -euo pipefail

ROOT="${NOVENA_HUB_ROOT:-/home/shouheng/Novena-Platform/Novena-Hub}"
cd "$ROOT"

stop_matching_processes() {
  local label="$1"
  local pattern="$2"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    return
  fi
  echo "Stopping stale $label process(es): $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "Force-stopping stale $label process(es): $pids"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
}

stop_stale_services() {
  stop_matching_processes "Django" "manage.py runserver 0.0.0.0:8000"
  stop_matching_processes "Celery" "celery -A novena_hub worker"
  stop_matching_processes "MQTT consumer" "manage.py mqtt_consumer"
  stop_matching_processes "Vite" "vite --host 0.0.0.0"
  stop_matching_processes "Mosquitto replay broker" "mosquitto -c $ROOT/mosquitto/wsl-lan-test.conf"
}

start_service() {
  local name="$1"
  shift

  mkdir -p "$ROOT/.dev-pids"
  if [[ -f "$ROOT/.dev-pids/$name.pid" ]]; then
    local old_pid
    old_pid="$(cat "$ROOT/.dev-pids/$name.pid")"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      kill "$old_pid" 2>/dev/null || true
      sleep 1
    fi
  fi

  setsid "$@" > "$ROOT/$name.log" 2> "$ROOT/$name.err.log" < /dev/null &
  echo "$!" > "$ROOT/.dev-pids/$name.pid"
  echo "$name pid $(cat "$ROOT/.dev-pids/$name.pid")"
}

stop_stale_services
start_service "mosquitto-wsl" /usr/sbin/mosquitto -c "$ROOT/mosquitto/wsl-lan-test.conf" -v
start_service "django-wsl" "/home/shouheng/.venvs/novena/bin/python" manage.py runserver 0.0.0.0:8000 --noreload
start_service "celery-wsl" "/home/shouheng/.venvs/novena/bin/celery" -A novena_hub worker -l INFO -B --pool=solo
start_service "mqtt-consumer-wsl" "/home/shouheng/.venvs/novena/bin/python" manage.py mqtt_consumer
start_service "vite-wsl" npm run dev -- --host 0.0.0.0 --force
