# Ubuntu Local Dev Troubleshooting

This file kept its historical name so existing skill references keep working. The current development machine is Ubuntu desktop, not Windows plus WSL.

## Shell Usage

Use native Ubuntu shell commands directly:

```bash
cd /home/shouheng/Projects/Novena-Platform/Novena-Hub
.agents/skills/novena-local-dev/scripts/start-novena-local-dev.sh
```

Do not wrap local commands in `wsl.exe`, PowerShell, or old `/mnt/c/...` paths unless the task is explicitly about the previous Windows machine.

## Service Startup

Native Ubuntu development expects these services:

- PostgreSQL on `localhost:5432`
- Redis on `localhost:6379`
- Mosquitto on `localhost:1883`
- Django on `0.0.0.0:8000`
- Vite on `0.0.0.0:5173`
- Celery worker with beat enabled
- Django MQTT consumer

If PostgreSQL or Redis cannot be started non-interactively, run:

```bash
sudo service postgresql start
sudo service redis-server start
```

Then rerun:

```bash
.agents/skills/novena-local-dev/scripts/start-novena-local-dev.sh
```

## Ports

Use these checks on Ubuntu:

```bash
ss -ltnp | grep -E ':8000|:5173|:5432|:6379|:1883'
```

If a port is already occupied by an old Novena process, inspect PID files:

```bash
ls -la .dev-pids
cat .dev-pids/*.pid
```

The repo's `scripts/start_wsl_dev_stack.sh` kills and replaces only the PIDs it previously wrote in `.dev-pids`. The script name is historical; it is still the active local dev stack launcher.

## Logs

Read these first:

```bash
tail -n 80 django-wsl.err.log
tail -n 80 vite-wsl.err.log
tail -n 80 celery-wsl.err.log
tail -n 80 mqtt-consumer-wsl.err.log
tail -n 80 mosquitto-wsl.err.log
```

## Database

The local `.env` should use native Ubuntu hostnames:

```env
DATABASE_URL="postgresql://postgres:<local-password>@localhost:5432/novena_hub"
REDIS_URL="redis://localhost:6379"
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
```

Do not use Docker hostnames like `db` or `redis` for native Ubuntu development.

Check readiness:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py check
/home/shouheng/.venvs/novena/bin/python manage.py migrate --check
/home/shouheng/.venvs/novena/bin/python manage.py verify_timescale
```

If migrations are pending:

```bash
/home/shouheng/.venvs/novena/bin/python manage.py migrate
```

## Mosquitto

For local Ubuntu testing, use:

```bash
/usr/sbin/mosquitto -c /home/shouheng/Projects/Novena-Platform/Novena-Hub/mosquitto/wsl-lan-test.conf -v
```

That config binds listener `1883` to `0.0.0.0` with anonymous access for local development and hardware tests.

For the old Pi-facing Windows LAN setup, use the historical notes in `docs/local_development_machine_notes.md`; that is no longer the default local workflow.
