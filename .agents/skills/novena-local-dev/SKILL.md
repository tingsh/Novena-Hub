---
name: novena-local-dev
description: Start, restart, inspect, and troubleshoot the Novena Hub local development stack on the Ubuntu desktop. Use when the user asks to start Novena Hub development, bring up Django/Vite/PostgreSQL/TimescaleDB/Redis/Mosquitto/Celery/MQTT consumer, verify the database is ready for testing, diagnose localhost service startup, or prepare local hardware integration testing.
---

# Novena Local Dev

Use this skill for Novena Hub local development on the Ubuntu desktop.

## Key Rules

- Use native Ubuntu shell commands directly; do not wrap commands in `wsl.exe` or Windows PowerShell.
- Prefer the bundled scripts in this skill, then the repo script `scripts/start_wsl_dev_stack.sh`, over reconstructing long startup commands. The script name is historical; it now represents the local Ubuntu dev stack.
- Use the Ubuntu project path:
  ```bash
  cd /home/shouheng/Projects/Novena-Platform/Novena-Hub
  ```
- Use the Ubuntu Python environment:
  ```bash
  /home/shouheng/.venvs/novena/bin/python
  ```
- Do not use old Windows or WSL paths unless the user explicitly asks about the previous machine.

## Fast Start

From the Ubuntu shell or Codex shell, start the stack with:

```bash
cd /home/shouheng/Projects/Novena-Platform/Novena-Hub
.agents/skills/novena-local-dev/scripts/start-novena-local-dev.sh
```

Then verify readiness with:

```bash
.agents/skills/novena-local-dev/scripts/health-check.sh
```

Expected browser endpoints:

- Django: `http://localhost:8000/`
- Vite: `http://localhost:5173/`
- App signup/login/dashboard routes are served by Django on port `8000`.

## What The Startup Script Does

1. Checks the Ubuntu repo and virtualenv paths.
2. Starts PostgreSQL if port `5432` is not ready.
3. Starts Redis if `redis-cli ping` does not return `PONG`.
4. Runs database readiness checks:
   - `python manage.py check`
   - `python manage.py migrate --check`
   - `python manage.py verify_timescale`
5. Delegates app process startup to the repo's known-good script:
   ```bash
   scripts/start_wsl_dev_stack.sh
   ```

The repo script starts:

```bash
/usr/sbin/mosquitto -c /home/shouheng/Projects/Novena-Platform/Novena-Hub/mosquitto/wsl-lan-test.conf -v
/home/shouheng/.venvs/novena/bin/python manage.py runserver 0.0.0.0:8000 --noreload
/home/shouheng/.venvs/novena/bin/celery -A novena_hub worker -l INFO -B --pool=solo
/home/shouheng/.venvs/novena/bin/python manage.py mqtt_consumer
npm run dev -- --host 0.0.0.0 --force
```

Logs and PID files are written in the repo:

- `.dev-pids/*.pid`
- `django-wsl.log`, `django-wsl.err.log`
- `vite-wsl.log`, `vite-wsl.err.log`
- `celery-wsl.log`, `celery-wsl.err.log`
- `mosquitto-wsl.log`, `mosquitto-wsl.err.log`
- `mqtt-consumer-wsl.log`, `mqtt-consumer-wsl.err.log`

## Database Readiness

The local `.env` is expected to point Django at native Ubuntu services:

```env
DATABASE_URL="postgresql://postgres:<local-password>@localhost:5432/novena_hub"
REDIS_URL="redis://localhost:6379"
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
```

Treat the database as ready for local testing only when:

- PostgreSQL accepts connections on `localhost:5432`.
- `manage.py check` succeeds.
- `manage.py migrate --check` succeeds. If it reports pending migrations, run `manage.py migrate`.
- `manage.py verify_timescale` confirms `telemetry_telemetrydata` is a Timescale hypertable.

## Troubleshooting

Read `references/wsl-troubleshooting.md` when startup fails, ports are already occupied, services require `sudo`, or browser-localhost behavior differs from the shell checks. The filename is historical; the guidance now targets Ubuntu desktop.
